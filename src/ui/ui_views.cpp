#include "ui_views.h"

#include "ui_widgets.h"
#include "ui_window.h"
#include "actions/actions.h"
#include "actions/notify.h"
#include "actions/logcap.h"
#include "actions/reconcile.h"
#include "dlc/dlc_scan.h"
#include "gui_whitelist.h"
#include "monitor_identity.h"
#include "ui_notices.h"
#include "version.h"
#include "globals.h"
#include "gui.h"
#include "gui_mhc.h"   // MhcProfileMetadataPeakNits
#include "mhc_preview.h"   // MhcPreviewHolds
#include <algorithm>
#include <cctype>
#include <cfloat>
#include <cmath>
#include <shobjidl.h>
#include <cstdio>
#include <mutex>
#include <vector>

namespace ui {

namespace {

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

// The physical display (device path, else EDID id): follows it between live and remembered.
std::wstring KeyOf(const DisplayRow& r) {
    if (!r.settings.identity.devicePath.empty()) return r.settings.identity.devicePath;
    if (!r.settings.identity.edidId.empty()) return r.settings.identity.edidId;
    return L"#" + std::to_wstring(r.liveIndex);
}

}  // namespace

// One sidebar row: unique even when a live display and a remembered one share a connector (a same-model
// replacement) or remembered entries have no identity.
std::wstring RowIdOf(const DisplayRow& r) {
    return (r.liveIndex >= 0 ? std::wstring(L"L|") : L"P" + std::to_wstring(r.settings.slot) + L"|") + KeyOf(r);
}

namespace {

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

// The same rules as the identity re-attach (monitor_identity.cpp): a device path names a connector, not a
// unit, so it counts only with a compatible EDID id; an EDID id alone counts only when exactly one live
// display has it (identical twins with serial-less EDIDs share one).
int ResolveLive(const DisplayIdentity& id, int hint) {
    std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
    const auto& list = g_gui.monitorSettings;
    if (id.empty()) return hint >= 0 && hint < (int)list.size() && list[hint].identity.empty() ? hint : -1;
    auto byPath = [&](const MonitorSettings& ms) {
        return !id.devicePath.empty() && _wcsicmp(ms.identity.devicePath.c_str(), id.devicePath.c_str()) == 0 &&
               EdidIdsCompatible(ms.identity.edidId, id.edidId);
    };
    if (hint >= 0 && hint < (int)list.size() && byPath(list[hint])) return hint;
    for (int i = 0; i < (int)list.size(); i++) if (byPath(list[i])) return i;
    if (id.edidId.empty()) return -1;
    int found = -1;
    for (int i = 0; i < (int)list.size(); i++) {
        if (_wcsicmp(list[i].identity.edidId.c_str(), id.edidId.c_str()) != 0) continue;
        if (found >= 0) return -1;   // ambiguous
        found = i;
    }
    return found;
}

// Runs `fn` on the display's current live index, or says it is gone.
void RunOnDisplay(const DisplayIdentity& id, int hint, const std::function<void(int)>& fn) {
    const int mon = ResolveLive(id, hint);
    if (mon < 0) {
        notify::Post(notify::Sev::Warning, -1, L"ui", L"That display is no longer connected");
        return;
    }
    fn(mon);
}

void PostTo(const DisplayRow& r, std::function<void(int)> fn) {
    const DisplayIdentity id = r.settings.identity;
    const int hint = r.liveIndex;
    Post([id, hint, fn]() { RunOnDisplay(id, hint, fn); });
}

}  // namespace

void PostToDisplay(const DisplayRow& r, std::function<void(int)> fn) { PostTo(r, std::move(fn)); }

namespace {

// A file picked in a dialog, then applied: the dialog pumps messages (a display can come or go meanwhile),
// so the display is found after it closes, not before.
void PostPickThen(const DisplayRow& r, std::function<bool(std::wstring&)> pick, std::function<void(int, const std::wstring&)> apply) {
    const DisplayIdentity id = r.settings.identity;
    const int hint = r.liveIndex;
    Post([id, hint, pick, apply]() {
        std::wstring path;
        if (!pick(path)) return;
        RunOnDisplay(id, hint, [&](int mon) { apply(mon, path); });
    });
}

void Ask(ViewState& vs, std::string title, std::string detail, std::string action, std::function<void()> onConfirm) {
    vs.confirm = { std::move(title), std::move(detail), std::move(action), std::move(onConfirm) };
    vs.confirmOpenRequested = true;
}

void OpenWhitelist(ViewState& vs, int which) {
    vs.whitelist = which;
    vs.whitelistOpenRequested = true;
    vs.whitelistAdd[0] = 0;
}

// What the live layers are doing (the lights never claim more).
PathRuntime RuntimeOf(const Snapshot& snap) {
    PathRuntime rt;
    rt.suspended = snap.suspended;
    rt.running = snap.running;
    rt.hookMode = snap.hookMode;
    rt.hookActive = snap.hookActive;
    return rt;
}

// Text with its cap height centred on centerY, without taking layout space.
void TextAt(Font f, float sizeDip, ImU32 col, float x, float centerY, const char* text) {
    DrawText(ImGui::GetWindowDrawList(), f, sizeDip, x, centerY, col, text);
}

// ---------------------------------------------------------------------------------------------
// Rows: a caps label column, values left, action buttons right-aligned, everything centred on the
// row's middle line (text by its cap height). Values are shortened (middle ellipsis) so they never run
// under the buttons.

struct RowCtx { float x0, y0, h, x, right, limit; };

RowCtx BeginRow(const char* label, float hDip = kRowH) {
    const ImVec2 p(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float h = Snap(Px(hDip));
    const float right = p.x + ImGui::GetContentRegionAvail().x;
    TextAt(Font::Label, kSizeLabel, pal::InkDim, p.x, p.y + h * 0.5f, label);
    return { p.x, p.y, h, p.x + Px(kRowLabelW), right, right };
}

void RowText(RowCtx& r, const std::string& text, ImU32 col = pal::Ink, Font f = Font::Mono, float sizeDip = kSizeMono,
             const std::wstring& tooltip = {}) {
    const std::string shown = Ellipsize(f, sizeDip, text, r.limit - r.x);
    if (shown.empty()) return;
    const float w = DrawText(ImGui::GetWindowDrawList(), f, sizeDip, r.x, r.y0 + r.h * 0.5f, col, shown.c_str());
    if (!tooltip.empty() && ImGui::IsMouseHoveringRect(ImVec2(r.x, r.y0), ImVec2(r.x + w, r.y0 + r.h)))
        ImGui::SetTooltip("%s", Utf8(tooltip).c_str());
    r.x += w + Px(12);
}

void RowPlace(RowCtx& r, float widgetHDip = kControlH) {
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

// ---------------------------------------------------------------------------------------------
// Header / footer

// The header: brand, page tabs, and at the far end the live-layers switch (a global state, not a page).
void DrawHeader(const Snapshot& snap, ViewState& vs, bool busy) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float w = ImGui::GetContentRegionAvail().x;
    const float h = Snap(Px(kHeaderH)), cy = p.y + h * 0.5f;
    const bool sheet = Shape().sheetTabs;
    const float t = BevelPx();
    // The edge under the header: a hairline, or the top highlight of the property sheet below the tabs.
    if (sheet) dl->AddRectFilled(ImVec2(p.x, p.y + h - t), ImVec2(p.x + w, p.y + h), Col(pal::BevelHi));
    else dl->AddRectFilled(ImVec2(p.x, p.y + h - 1), ImVec2(p.x + w, p.y + h), Col(pal::Rule));

    // The logo artwork (mipmapped texture); the drawn cube only if the texture could not be made.
    const float logo = Snap(Px(34));
    const float lx = Snap(p.x + Px(kGutter));
    if (void* tex = LogoTexture())
        dl->AddImage(ImTextureRef((ImTextureID)(uintptr_t)tex), ImVec2(lx, Snap(cy - logo * 0.5f)),
                     ImVec2(lx + logo, Snap(cy - logo * 0.5f) + logo));
    else
        DrawCubeLogo(dl, ImVec2(lx + logo * 0.5f, cy), logo * 0.5f);
    const float textX = lx + logo + Px(12);
    const float nameW = DrawText(dl, Font::Label, 14.0f, textX, cy, pal::Ink, "DESKTOPLUT");
    const float tagW = DrawText(dl, Font::Label, kSizeLabel, textX + nameW + Px(12), cy, pal::InkFaint, "PREVIEW");

    // Page tabs: the first one's text starts on the main column's edge when the brand leaves room.
    struct PageTab { const char* id; const char* label; Page page; };
    static const PageTab kTabs[] = {
        { "##pg_pipe", "PIPELINE", Page::Pipeline },
        { "##pg_events", "EVENTS", Page::Events },
        { "##pg_settings", "SETTINGS", Page::Settings },
    };
    const float brandEnd = textX + nameW + Px(12) + tagW + Px(20);
    const float columnX = p.x + Px(kSidebarW) + Px(kGutter) - Px(18);   // a tab's text sits 18 DIP in
    float x = Snap(brandEnd > columnX ? brandEnd : columnX);
    const float tabTop = sheet ? p.y + Px(14) : p.y;
    const float tabBottom = sheet ? p.y + h - t : p.y + h;
    float tabX[3];
    for (int i = 0; i < 3; i++) {
        tabX[i] = x;
        x += TabWidth(kTabs[i].label) + (sheet ? Px(2) : 0.0f);
    }
    // The selected tab last: a property-sheet tab overlaps its neighbours.
    const Page shownPage = vs.page;
    for (int pass = 0; pass < 2; pass++) {
        for (int i = 0; i < 3; i++) {
            const PageTab& tab = kTabs[i];
            if ((tab.page == shownPage) != (pass == 1)) continue;
            const float tw = TabWidth(tab.label);
            if (Tab(tab.id, tab.label, shownPage == tab.page, ImVec2(tabX[i], tabTop), ImVec2(tabX[i] + tw, tabBottom))) {
                if (tab.page == Page::Settings && vs.page != Page::Settings)
                    Post([]() { RefreshStartupEnabledCache(); });   // COM: not in a frame
                vs.page = tab.page;
            }
        }
    }

    // Live layers: on = 3D LUT, tone mapping and FALD run; off = suspended (MHC stays on).
    const bool live = !snap.suspended;
    const char* label = "LIVE LAYERS";
    const float sw = SwitchWidth(label);
    ImGui::SetCursorScreenPos(ImVec2(Snap(p.x + w - Px(kGutter) - sw), Snap(cy - Px(kControlH) * 0.5f)));
    ImGui::BeginDisabled(busy || (snap.calibrating && live));
    if (Switch("##live", label, live)) Post([live]() { act::SetLiveLayersSuspended(live); });
    ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip(snap.calibrating && live ? "Calibration in progress."
                          : live ? "3D LUT, tone mapping and FALD. Off suspends them; MHC stays on."
                                 : "Suspended: 3D LUT, tone mapping and FALD off. MHC stays on.");

    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h));
    ImGui::Dummy(ImVec2(0, 0));
}

// The footer is pinned to the window's bottom edge (not placed after the body), so spacing above it can
// never push it down or clip it.
float FooterTop() {
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    return Snap(vp->WorkPos.y + vp->WorkSize.y - Px(kFooterH));
}

void DrawFooter(const Snapshot& snap, ViewState& vs, bool busy) {
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    const ImVec2 p(vp->WorkPos.x, FooterTop());
    const float w = vp->WorkSize.x;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float h = Snap(Px(kFooterH)), cy = p.y + h * 0.5f;
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
    const char* stateText = snap.suspended ? "SUSPENDED" : nullptr;
    if (Shape().bevel) {
        // A Windows 98 status bar: two shallow sunken panes.
        const float t = BevelPx(), in = Snap(Px(3));
        dl->AddRectFilled(ImVec2(p.x, p.y), ImVec2(p.x + w, p.y + t), Col(pal::BevelHi));
        auto pane = [&](float x0, float x1) {
            const ImVec2 a(Snap(x0), p.y + in), b(Snap(x1), p.y + h - in);
            dl->AddRectFilled(a, ImVec2(b.x, a.y + t), Col(pal::BevelShadow));
            dl->AddRectFilled(a, ImVec2(a.x + t, b.y), Col(pal::BevelShadow));
            dl->AddRectFilled(ImVec2(a.x, b.y - t), b, Col(pal::BevelHi));
            dl->AddRectFilled(ImVec2(b.x - t, a.y), b, Col(pal::BevelHi));
        };
        const float split = p.x + Px(kSidebarW);
        pane(p.x + in, split - Px(2));
        pane(split + Px(2), p.x + w - in);
        x = p.x + Px(12);
    } else {
        dl->AddRectFilled(p, ImVec2(p.x + w, p.y + 1), Col(pal::Rule));
    }
    const float d = Snap(Px(8));
    const ImVec2 c(Snap(x) + d * 0.5f, Snap(cy));
    if (state == Good && Shape().bevel) DrawLight(dl, c, Light::Configured, d, pal::Good);   // Windows 98: a green check
    else if (state == Good) dl->AddCircleFilled(c, d * 0.5f, Col(pal::Good));
    else if (state == Fault) DrawLight(dl, c, Light::Fault, d);
    else DrawLight(dl, c, Light::Off, d);
    x += d + Px(8);
    x += DrawText(dl, Font::Label, kSizeLabel, x, cy, state == Off ? pal::InkFaint : pal::InkDim, what) + Px(10);
    if (stateText) DrawText(dl, Font::Label, kSizeLabel, x, cy, pal::Ink, stateText);
    x = Shape().bevel ? p.x + Px(kSidebarW) + Px(12) : p.x + Px(kSidebarW) + Px(kGutter);
    if (busy) {
        DrawText(dl, Font::Label, kSizeLabel, x, cy, pal::Ink, "APPLYING");
    } else if (!snap.lastStatus.empty()) {
        // The newest status, a warning in amber; a click opens Events (its detail is there).
        const bool warn = snap.lastStatusSev == notify::Sev::Warning || snap.lastStatusSev == notify::Sev::Error;
        const std::string text = Utf8(snap.lastStatus);
        const float tw = MeasureInk(Font::Mono, 12.0f, text.c_str()).advance;
        ImGui::SetCursorScreenPos(ImVec2(x, p.y));
        ImGui::InvisibleButton("##footer_status", ImVec2(std::max(tw, 1.0f), h));
        const bool hot = ImGui::IsItemHovered();
        if (ImGui::IsItemClicked()) vs.page = Page::Events;
        if (hot) ImGui::SetTooltip("Events");
        DrawText(dl, Font::Mono, 12.0f, x, cy, warn ? pal::Alarm : (hot ? pal::InkDim : pal::InkFaint), text.c_str());
    }
}

// ---------------------------------------------------------------------------------------------
// Sidebar

const DisplayRow* FindSelected(const Snapshot& snap, ViewState& vs) {
    for (const DisplayRow& r : snap.live) if (RowIdOf(r) == vs.selectedKey) return &r;
    for (const DisplayRow& r : snap.parked) if (RowIdOf(r) == vs.selectedKey) return &r;
    // The selected display was connected or disconnected meanwhile: follow it by identity.
    const DisplayRow* pick = nullptr;
    if (vs.selectedKey.size() > 2) {
        const std::wstring key = vs.selectedKey.substr(vs.selectedKey.find(L'|') + 1);
        for (const DisplayRow& r : snap.live) if (!pick && KeyOf(r) == key) pick = &r;
        for (const DisplayRow& r : snap.parked) if (!pick && KeyOf(r) == key) pick = &r;
    }
    // Default: the primary display, else the first one.
    if (!pick) for (const DisplayRow& r : snap.live) if (r.primary) { pick = &r; break; }
    if (!pick && !snap.live.empty()) pick = &snap.live.front();
    if (!pick && !snap.parked.empty()) pick = &snap.parked.front();
    if (pick) vs.selectedKey = RowIdOf(*pick);
    return pick;
}

void CommitRename(ViewState& vs, const DisplayRow& r) {
    vs.renamingKey.clear();
    const std::wstring label = Wide(vs.renameBuf);
    if (label == r.name) return;   // untouched: do not pin today's automatic name as a label
    const int slot = r.settings.slot, live = r.liveIndex;
    Post([slot, live, label]() { act::SetDisplayLabel(slot, live, label); });
}

void SidebarRow(const DisplayRow& r, bool selected, ViewState& vs, const PathRuntime& rt) {
    const float w = ImGui::GetContentRegionAvail().x;
    const bool live = r.liveIndex >= 0;
    const float h = Snap(Px(live ? 60.0f : 44.0f));
    const ImVec2 p(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const std::wstring key = RowIdOf(r);
    const bool renaming = vs.renamingKey == key;
    const bool list = Shape().sunkenSidebar;   // a white list box with a navy selection (Light)
    ImGui::PushID(Utf8(key).c_str());
    bool hovered = false;
    if (renaming) {
        ImGui::Dummy(ImVec2(w, h));   // the name field below takes the input
    } else {
        if (ImGui::InvisibleButton("##row", ImVec2(w, h))) {
            vs.selectedKey = key;
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
    // On a list selection (navy in Light) every mark takes the selection's ink. While the page waits (an editor
    // holds it) Light shows Windows 98's inactive selection, unfaded: a grey bar with white text.
    const bool onSel = selected && list;
    const bool inactiveSel = onSel && (ImGui::GetItemFlags() & ImGuiItemFlags_Disabled) != 0;
    if (inactiveSel) ImGui::PushStyleVar(ImGuiStyleVar_Alpha, 1.0f);
    const ImU32 selInk = inactiveSel ? pal::BevelHi : pal::ListSelInk;
    const ImU32 ink = onSel ? selInk : (live ? pal::Ink : pal::InkFaint);
    const ImU32 dim = onSel ? selInk : pal::InkDim;
    const bool cards = Shape().cardSidebar;   // cards (Gray)
    if (cards) {
        const float m = Snap(Px(10)), gy = Snap(Px(3)), rr = Snap(Px(Shape().panelRounding));
        const ImVec2 ca(p.x + m, p.y + gy), cb(p.x + w - m, p.y + h - gy);
        dl->AddRectFilled(ca, cb, Col(selected ? pal::ListSel : (hovered ? pal::Hover : pal::Panel)), rr);
        dl->AddRect(ca, cb, Col(selected ? pal::Accent : pal::PanelEdge), rr);
    } else if (selected) {
        dl->AddRectFilled(p, ImVec2(p.x + w, p.y + h), Col(inactiveSel ? pal::BevelShadow : pal::ListSel));
        if (!list) dl->AddRectFilled(p, ImVec2(p.x + Snap(Px(3)), p.y + h), Col(pal::Accent));
    } else if (hovered && !list) {
        dl->AddRectFilled(p, ImVec2(p.x + w, p.y + h), Col(pal::Hover));
    }
    if (!list && !cards) dl->AddRectFilled(ImVec2(p.x, p.y + h - 1), ImVec2(p.x + w, p.y + h), Col(pal::Rule));

    const float pad = Px(list ? 12.0f : (cards ? 22.0f : kGutter));
    const float nameY = p.y + (live ? Px(21) : h * 0.5f);
    const char* badge = live ? ModeBadge(r.mode) : "OFFLINE";
    const float badgeW = MeasureInk(Font::Label, kSizeLabel, badge).advance;
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
        TextAt(selected ? Font::BodyBold : Font::Body, kSizeBody, ink, p.x + pad, nameY, name.c_str());
    }

    if (live) {
        // Layer lights for the live mode's side, by the pixel path's rules: calibration (both MHC stages), 3D LUT;
        // centred on the resolution's digits. Nothing set = no light (the place stays).
        Light cal = Light::None, lut = Light::None;
        PathRuntime rowRt = rt;
        rowRt.editHeld = r.liveIndex >= 0 && MhcPreviewHolds(r.liveIndex, PipelineIsHdr(r.mode));
        for (const Stage& s : BuildPixelPath(r.settings, r.mode, r.mode, rowRt)) {
            if (s.id == StageId::Primaries || s.id == StageId::Grayscale) cal = CombineLights(cal, s.light);
            else if (s.id == StageId::Lut3D) lut = s.light;
        }
        const float cy = p.y + Px(42);
        const float d = Snap(Px(7));
        float x = Snap(p.x + pad);
        for (Light l : { cal, lut }) {
            DrawLight(dl, ImVec2(x + d * 0.5f, Snap(cy)), l, d, onSel ? selInk : 0);
            x += Shape().bevel ? Snap(d * 1.45f + Px(6)) : d + Snap(Px(5));   // Windows 98 checks are wider than dots
        }
        if (r.widthPx > 0) {
            char res[32];
            snprintf(res, sizeof(res), "%d\xC3\x97%d", r.widthPx, r.heightPx);
            TextAt(Font::Mono, 12.0f, dim, x + Px(4), cy, res);
        }
        if (r.reminderDue) {   // right-aligned under the mode badge: nothing else moves
            const char* due = "RECAL";
            const float dueW = MeasureInk(Font::Label, kSizeLabel, due).advance;
            TextAt(Font::Label, kSizeLabel, onSel ? selInk : pal::Alarm, p.x + w - pad - dueW, cy, due);
        }
    }
    if (inactiveSel) ImGui::PopStyleVar();
    ImGui::PopID();
    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h));
    ImGui::Dummy(ImVec2(0, 0));
}

void DrawSidebar(const Snapshot& snap, ViewState& vs, const DisplayRow* sel) {
    if (Shape().cardSidebar) ImGui::Dummy(ImVec2(0, Px(7)));
    const PathRuntime rt = RuntimeOf(snap);
    for (const DisplayRow& r : snap.live) SidebarRow(r, sel == &r, vs, rt);
    for (const DisplayRow& r : snap.parked) SidebarRow(r, sel == &r, vs, rt);
}

// ---------------------------------------------------------------------------------------------
// Pixel path: the stages as nodes on the signal line, from the desktop to the panel (Gray: in a
// strip, chevrons between the stages).

void DrawPixelPath(const std::vector<Stage>& path, ViewState& vs) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float h = Snap(Px(34));
    const bool strip = Shape().pathStrip;
    const float pad = strip ? Snap(Px(8)) : 0.0f;
    const ImVec2 outer(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float outerW = ImGui::GetContentRegionAvail().x;
    const ImVec2 p0(outer.x + pad, outer.y + pad);
    const float avail = outerW - 2.0f * pad;
    const float midY = p0.y + h * 0.5f;
    if (strip) {
        const float r = Snap(Px(Shape().panelRounding));
        dl->AddRectFilled(outer, ImVec2(outer.x + outerW, outer.y + h + 2 * pad), Col(pal::Raised), r);
        dl->AddRect(outer, ImVec2(outer.x + outerW, outer.y + h + 2 * pad), Col(pal::Rule), r);
    }

    // The layout that fits, best first; the lights are the last thing to go:
    //   0  equal nodes (as wide as fits, capped), markers labelled, chevrons (Gray)
    //   1  nodes as wide as their own label and light
    //   2  the markers lose their labels (the line ends and the scan-out cut stay), no chevrons
    //   3  no lights (only below the minimum window)
    // The gaps between all elements are equal.
    const size_t n = path.size();
    std::vector<float> label(n, 0.0f);
    float widestLabel = 0.0f;
    int stages = 0;
    for (size_t i = 0; i < n; i++) {
        label[i] = MeasureInk(Font::Label, kSizeLabel, StageLabel(path[i].id)).advance;
        if (path[i].selectable) { stages++; widestLabel = label[i] > widestLabel ? label[i] : widestLabel; }
    }
    const float nodePad = Px(22), lightW = Px(7 + 7);
    std::vector<float> widths(n, 0.0f);
    int level = 0;
    float gap = 0.0f;
    for (; level <= 3; level++) {
        const bool compactMarkers = level >= 2;
        const float minGap = Px(strip && !compactMarkers ? 26.0f : (compactMarkers ? 6.0f : 12.0f));
        float markers = 0.0f;
        for (size_t i = 0; i < n; i++)
            if (!path[i].selectable) { widths[i] = compactMarkers ? Px(8) : label[i] + Px(16); markers += widths[i]; }
        const float light = level <= 2 ? lightW : 0.0f;
        float nodes = 0.0f;
        if (level == 0) {
            float stageW = stages ? (avail - markers - minGap * (float)(n - 1)) / (float)stages : 0.0f;
            if (stageW < widestLabel + light + nodePad) continue;
            stageW = Snap(stageW > Px(132) ? Px(132) : stageW);
            for (size_t i = 0; i < n; i++) if (path[i].selectable) { widths[i] = stageW; nodes += stageW; }
        } else {
            for (size_t i = 0; i < n; i++)
                if (path[i].selectable) { widths[i] = Snap(label[i] + light + nodePad); nodes += widths[i]; }
            if (level < 3 && markers + nodes + minGap * (float)(n - 1) > avail) continue;
        }
        gap = n > 1 ? (avail - markers - nodes) / (float)(n - 1) : 0.0f;
        break;
    }
    const bool lights = level <= 2;
    const bool markerLabels = level <= 1;
    const bool chevrons = strip && level <= 1;

    // The line: only in the gaps between the elements (and through unlabelled markers), never behind a node or a
    // label, so a node greyed out (translucent) never shows it through.
    if (!strip) {
        float lx = p0.x;
        for (size_t i = 0; i < n; i++) {
            const bool through = !path[i].selectable && !markerLabels;
            const float x0 = through ? lx : lx + widths[i];
            const float x1 = i + 1 < n ? lx + widths[i] + gap : (through ? lx + widths[i] : x0);
            if (x1 > x0) dl->AddRectFilled(ImVec2(Snap(x0), Snap(midY)), ImVec2(Snap(x1), Snap(midY) + 1), Col(pal::RuleStrong));
            lx += widths[i] + gap;
        }
    }

    float x = p0.x;
    for (size_t i = 0; i < path.size(); i++) {
        const Stage& s = path[i];
        const float w = widths[i];
        if (!s.selectable) {
            const char* t = StageLabel(s.id);
            const InkBox ib = MeasureInk(Font::Label, kSizeLabel, t);
            const float halfH = markerLabels ? (ib.baseline - ib.capTop) * 0.5f + Px(4) : 0.0f;
            if (!markerLabels) {
                // Compact: only the scan-out cut stays (the line itself shows where the path starts and ends).
                if (s.id == StageId::Scanout) {
                    const float cx = Snap(x + w * 0.5f);
                    for (float yy = strip ? p0.y : p0.y - Px(6); yy < p0.y + h + (strip ? 0.0f : Px(6)); yy += Px(6))
                        dl->AddRectFilled(ImVec2(cx, Snap(yy)), ImVec2(cx + 1, Snap(yy + Px(3))), Col(pal::InkFaint));
                }
                x += w + gap;
                continue;
            }
            if (strip && s.id != StageId::Scanout)   // the ends as quiet chips
                dl->AddRectFilled(ImVec2(Snap(x), p0.y), ImVec2(Snap(x + w), p0.y + h), Col(pal::Panel), Snap(Px(Shape().controlRounding)));
            if (s.id == StageId::Scanout) {
                // The cut: dashes above and below the label, never through it.
                const float gapTop = midY - halfH - Px(2), gapBottom = midY + halfH + Px(2);
                const float cx = Snap(x + w * 0.5f);
                for (float yy = strip ? p0.y : p0.y - Px(6); yy < gapTop; yy += Px(6))
                    dl->AddRectFilled(ImVec2(cx, Snap(yy)), ImVec2(cx + 1, Snap(yy + Px(3) < gapTop ? yy + Px(3) : gapTop)),
                                      Col(pal::InkFaint));
                for (float yy = gapBottom; yy < p0.y + h + (strip ? 0.0f : Px(6)); yy += Px(6))
                    dl->AddRectFilled(ImVec2(cx, Snap(yy)), ImVec2(cx + 1, Snap(yy + Px(3))), Col(pal::InkFaint));
            }
            DrawTextIn(dl, Font::Label, kSizeLabel, ImVec2(x, p0.y), ImVec2(x + w, p0.y + h), pal::InkFaint, t);
        } else {
            ImGui::SetCursorScreenPos(ImVec2(Snap(x), p0.y));
            char id[32];
            snprintf(id, sizeof(id), "##stage%d", (int)s.id);
            const bool selected = vs.stage == s.id;
            const bool off = s.dimmed;   // not changing the picture (nothing set, switched off, suspended)
            if (Node(id, StageLabel(s.id), s.light, selected, off, ImVec2(w / Dpi(), 34), lights)) vs.stage = s.id;
        }
        // Gray: a chevron in the gap after each element (not into or out of the scan-out cut).
        if (chevrons && i + 1 < path.size() && s.id != StageId::Scanout && path[i + 1].id != StageId::Scanout) {
            const float cx = x + w + gap * 0.5f, s3 = Px(3.5f);
            const ImVec2 pts[3] = { ImVec2(cx - s3 * 0.5f, midY - s3), ImVec2(cx + s3 * 0.5f, midY), ImVec2(cx - s3 * 0.5f, midY + s3) };
            dl->AddPolyline(pts, 3, Col(pal::RuleStrong), Px(1.5f));
        }
        x += w + gap;
    }
    ImGui::SetCursorScreenPos(ImVec2(outer.x, outer.y + h + 2 * pad));
    ImGui::Dummy(ImVec2(outerW, 0));
}

// ---------------------------------------------------------------------------------------------
// Inspectors (each fills the panel the selected node opens; the panel carries the title and light)

const char* InspectorTitle(StageId id) {
    switch (id) {
    case StageId::Lut3D:       return "3D LUT";
    case StageId::ToneMap:     return "TONE MAP";
    case StageId::Fald:        return "FALD";
    case StageId::ShaderTweak: return "SHADER TWEAK";
    case StageId::Primaries:   return "PRIMARIES & WHITE";
    case StageId::Grayscale:   return "GRAYSCALE & GAMMA";
    default:                   return "";
    }
}

void InspectLut(const Snapshot& snap, const DisplayRow& r, const Stage& st, bool hdr) {
    const std::wstring& path = hdr ? r.settings.hdrPath : r.settings.sdrPath;

    RowCtx row = BeginRow("CUBE");
    std::vector<const char*> buttons = { "BROWSE\xE2\x80\xA6" };
    if (!path.empty()) buttons.push_back("CLEAR");
    const int b = RowButtons(row, "lut", buttons);
    if (b == 0)
        PostPickThen(r,
                     [](std::wstring& out) {
                         wchar_t file[MAX_PATH] = {};
                         if (!BrowseForLUT(Window(), file, MAX_PATH)) return false;
                         out = file;
                         return true;
                     },
                     [hdr](int mon, const std::wstring& file) { act::SetLutPath(mon, hdr, file); });
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
    auto post = [&r](TonemapSettings t) { PostTo(r, [t](int mon) { act::SetTonemap(mon, t); }); };

    RowCtx row = BeginRow("CURVE");
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
    if (OnOff("##tm_dyn", tm.dynamicPeak, &dyn, "DYNAMIC", "FIXED", 84.0f)) { TonemapSettings t = tm; t.dynamicPeak = dyn; post(t); }
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

    // What Windows tells games and apps the peak is (windows_tonemap.h), and what it reports now: 10000 nits leaves
    // the highlights to DesktopLUT's tone map, the panel's peak lets them tone map themselves.
    const MHCSettings& hm = r.settings.hdrMHC;
    const bool ownProfile = hm.enabled && !hm.profileName.empty();   // it carries the peak (else the override)
    const float panelNits = ownProfile ? MhcProfileMetadataPeakNits(hm, true) : r.edidPeakNits;
    row = BeginRow("WINDOWS TONE MAP");
    {
        std::string tip = "Peak Windows reports to games and apps.\n"
                          "Auto: 10000 nits while DesktopLUT tone maps the full range (dynamic, or content peak "
                          "10000), else the panel's.\n"
                          "Bypass: always 10000 nits; Windows and apps never tone map.\n"
                          "Panel: the panel's peak; apps tone map themselves.";
        // The profile's peak is the panel's when it was measured (an imported file or a base curve), else a default.
        const bool measured = !hm.sourceFilePath.empty() || (hm.baseGrayscale.enabled && !hm.baseGrayscale.points.empty());
        if (panelNits > 0.0f)
            tip += Fmt(!ownProfile ? "\nPanel peak: %.0f nits (EDID)." : measured ? "\nPanel peak: %.0f nits."
                                                                                  : "\nProfile peak: %.0f nits.", panelNits);
        RowTip(row, tip.c_str());
    }
    RowPlace(row);
    static const char* kWinToneMap[] = { "AUTO", "BYPASS", "PANEL" };
    const int mode = Segmented("##wtm", kWinToneMap, 3, (int)r.settings.windowsToneMap, 72);
    if (mode >= 0) {
        const WindowsToneMap m = (WindowsToneMap)mode;
        PostTo(r, [m](int mon) { act::SetWindowsToneMap(mon, m); });
    }
    RowNext(row);
    {
        const float reported = r.reportedPeakNits;   // 0: unknown, or the display is not in HDR
        if (reported > 0.0f) {
            // Without a profile of ours the panel's value is Windows' own (or another tool's profile): not judged.
            const float expected = hm.reportPeakMax ? kWindowsToneMapBypassNits : ownProfile ? panelNits : 0.0f;
            const bool off = expected > 0.0f && std::fabs(reported - expected) > 1.0f;
            wchar_t tip[200] = {};
            if (off)
                swprintf_s(tip, L"Windows reports %.0f, not %.0f nits: being applied, waiting for a calibration or an "
                                L"edit to end, or another HDR profile replaced it.", reported, expected);
            // The unit in a fixed place (the widest reading), so it does not move as the reading changes.
            const float unitX = row.x + MeasureText(Font::Mono, kSizeMono, "10000").x + Px(12);
            RowText(row, Fmt("%.0f", reported), off ? pal::Alarm : pal::Ink, Font::Mono, kSizeMono, tip);
            row.x = (std::max)(row.x, unitX);
            RowText(row, "nits", pal::InkDim, Font::Label, kSizeLabel);
        } else {
            RowText(row, "\xE2\x80\x94", pal::InkFaint);
        }
    }
    EndRow(row);
}

void InspectFald(const DisplayRow& r, const Stage& st, bool hdr) {
    const FaldSettings& f = hdr ? r.settings.hdrColorCorrection.fald : r.settings.sdrColorCorrection.fald;

    RowCtx row = BeginRow("PANEL FILE");
    const int b = RowButtons(row, "fald", { "BROWSE\xE2\x80\xA6", "RELOAD" });
    if (b == 0)
        PostPickThen(r,
                     [](std::wstring& out) {
                         wchar_t file[1024] = {};
                         if (!BrowseForFaldPanelFile(Window(), file, 1024)) return false;
                         out = file;
                         return true;
                     },
                     [hdr](int mon, const std::wstring& file) { act::SetFaldPanelFile(mon, hdr, file, true); });
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
    RowCtx row = BeginRow("PRIMARIES"); RowText(row, OnOffText(cc.primariesEnabled)); EndRow(row);
    row = BeginRow("GRAYSCALE"); RowText(row, OnOffText(cc.grayscale.enabled)); EndRow(row);
    row = BeginRow("GAMMA 2.4"); RowText(row, OnOffText(cc.grayscale.use24Gamma)); EndRow(row);
}

void InspectPrimaries(const DisplayRow& r, const Stage& st, bool hdr, ViewState& vs) {
    const MHCSettings& mhc = hdr ? r.settings.hdrMHC : r.settings.sdrMHC;
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
        OpenMhcEditor(vs, r, hdr);
    } else if (b == 1 && installed) {
        const DisplayRow target = r;
        Ask(vs, "REMOVE PROFILE?", "Deletes the calibration profile and its cached variants.", "REMOVE",
            [target, hdr]() {
                RunOnDisplay(target.settings.identity, target.liveIndex, [hdr](int mon) { act::MhcRemove(mon, hdr); });
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

    // The profile's 1D LUT holds two curves, both edited live in the same editor: the base, and the fine-tune on top.
    // An edit previews exactly what the bake installs: it needs that profile, and the display in this mode.
    const bool inMode = r.mode == Mode::Unknown || (r.mode == Mode::HDR) == hdr;
    const char* cannot = mhc.bypassGrayscale ? nullptr
                       : r.liveIndex < 0 ? "Display not connected."
                       : mhc.profileName.empty() ? "Needs an installed MHC profile."
                       : !inMode ? (hdr ? "Display is in SDR." : "Display is in HDR.") : nullptr;

    RowCtx row = BeginRow("BASE");
    if (!mhc.sourceFilePath.empty()) {
        // An imported file supplies the base (the curve's points are then unused by the bake).
        RowTip(row, "From the imported file (Primaries \xE2\x80\xBA EDIT\xE2\x80\xA6 chooses it).");
        RowText(row, mhc.sourceIs1DCube ? "1D cube" : "ICC curves");
        RowText(row, FileName(mhc.sourceFilePath), pal::InkDim, Font::Mono, kSizeMono, mhc.sourceFilePath);
    } else {
        if (cannot) RowTip(row, cannot);
        ImGui::BeginDisabled(mhc.bypassGrayscale || cannot != nullptr);
        if (RowButtons(row, "base", { "EDIT CURVE\xE2\x80\xA6" }) == 0) OpenGsEditor(vs, r, hdr, GsTarget::Base);
        ImGui::EndDisabled();
        RowText(row, mhc.baseGrayscale.enabled && !mhc.baseGrayscale.points.empty()
                         ? Fmt("%.0f points", mhc.baseGrayscale.pointCount) : std::string("standard"));
    }
    EndRow(row);

    row = BeginRow("FINE-TUNE");
    if (cannot) RowTip(row, cannot);
    ImGui::BeginDisabled(mhc.bypassGrayscale || cannot != nullptr);
    const int b = RowButtons(row, "gs", { "EDIT CURVE\xE2\x80\xA6", "RESET" });
    ImGui::EndDisabled();
    if (b == 0) {
        OpenGsEditor(vs, r, hdr);
    } else if (b == 1) {
        const DisplayRow target = r;
        Ask(vs, "RESET FINE-TUNE?", "Returns every point to neutral.", "RESET", [target, hdr]() {
            RunOnDisplay(target.settings.identity, target.liveIndex, [hdr](int mon) { act::MhcResetCorrectionGs(mon, hdr); });
        });
    }
    RowPlace(row);
    bool on = false;
    if (OnOff("##gs_on", gs.enabled, &on)) PostTo(r, [hdr, on](int mon) { act::MhcSetCorrectionGsEnabled(mon, hdr, on); });
    RowNext(row);
    RowText(row, Fmt("%.0f points", gs.pointCount), pal::InkDim);   // the count is set in the editor
    EndRow(row);

    if (hdr) {
        row = BeginRow("PEAK");
        RowTip(row, "Where the HDR points sit: the profile's peak (Primaries \xE2\x80\xBA EDIT\xE2\x80\xA6 sets it).");
        RowText(row, Fmt("%.0f nits", MhcProfileMetadataPeakNits(mhc, true)), pal::InkDim);
        EndRow(row);

        row = BeginRow("DESKTOP GAMMA");
        RowTip(row, "SDR content in HDR as gamma 2.2 at the Windows SDR white level.\n"
                    "The whitelist turns it off while a listed app runs.");
        if (RowButtons(row, "dg_wl", { "WHITELIST" }) == 0) OpenWhitelist(vs, 1);
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

void DrawInspector(const Snapshot& snap, const DisplayRow& r, const Stage& st, bool hdr, ViewState& vs) {
    switch (st.id) {
    case StageId::Lut3D:       InspectLut(snap, r, st, hdr); break;
    case StageId::ToneMap:     InspectToneMap(r, st); break;
    case StageId::Fald:        InspectFald(r, st, hdr); break;
    case StageId::ShaderTweak: InspectShaderTweak(r, st, hdr); break;
    case StageId::Primaries:   InspectPrimaries(r, st, hdr, vs); break;
    case StageId::Grayscale:   InspectGrayscale(r, st, hdr, vs); break;
    default: break;
    }
}

// ---------------------------------------------------------------------------------------------
// Measured panel: what DLC recorded and measured for the selected display and the mode shown. Its rows
// keep the inspector's pitch, so the two panels' rows line up.

constexpr float kMeasuredLabelW = 84.0f;

// A row of the measured panel: label, value (ellipsized), optional tooltip on the row.
void MeasuredRow(const char* label, const std::string& value, ImU32 col = pal::Ink, Font f = Font::Mono,
                 const char* tip = nullptr) {
    RowCtx row = BeginRow("", kRowH);
    TextAt(Font::Label, kSizeLabel, pal::InkDim, row.x0, row.y0 + row.h * 0.5f, label);
    row.x = row.x0 + Px(kMeasuredLabelW);
    RowText(row, value, col, f, f == Font::Mono ? kSizeMono : kSizeLabel);
    if (tip && ImGui::IsMouseHoveringRect(ImVec2(row.x0, row.y0), ImVec2(row.right, row.y0 + row.h))) ImGui::SetTooltip("%s", tip);
    EndRow(row);
}

// Readouts: the verification's numbers as label-over-value tiles, three to a row, two rows to three
// row pitches.
struct Readout { const char* label; std::string value; ImU32 col; };
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

const char* LayerText(dlc::LayerState s, bool bypassed = false) {
    if (bypassed) return "BYPASSED";
    switch (s) {
    case dlc::LayerState::Matches: return "AS APPLIED";
    case dlc::LayerState::Differs: return "CHANGED";
    case dlc::LayerState::Unrecorded: return "NOT FROM DLC";
    default: return "OFF";
    }
}

ImU32 LayerColour(dlc::LayerState s) {
    if (s == dlc::LayerState::Matches) return pal::Ink;
    if (s == dlc::LayerState::Differs) return pal::Alarm;
    return pal::InkFaint;
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

// The measured panel's light: verified (and not due), a fault when only an older report exists or a
// reminder is due, off when nothing is recorded.
Light MeasuredLight(const DisplayRow& r, bool showHdr) {
    const dlc::ModeRecord& rec = r.dlcRecord[showHdr ? 1 : 0];
    if (rec.current >= 0) return rec.reminderDue ? Light::Fault : Light::Configured;
    if (rec.latest >= 0) return Light::Fault;
    return Light::None;
}

void DrawMeasured(const Snapshot& snap, const DisplayRow& r, bool showHdr, ViewState& vs) {
    const dlc::Data& d = *snap.dlc;
    const int mi = showHdr ? 1 : 0;
    const dlc::ModeRecord& rec = r.dlcRecord[mi];
    const int shown = rec.current >= 0 ? rec.current : rec.latest;

    if (d.folder.empty()) {
        MeasuredRow("DLC", "NO FOLDER", pal::InkFaint, Font::Label, "No DLC folder set or found. Settings > Calibration.");
        RowCtx row = BeginRow("", kRowH);
        row.x = row.x0 + Px(kMeasuredLabelW);
        RowPlace(row);
        if (Button("##meas_settings", "SETTINGS", kButtonW)) vs.page = Page::Settings;
        EndRow(row);
        return;
    }
    if (r.dlcMatch.name.empty()) {
        MeasuredRow("DLC", r.dlcMatch.ambiguous ? "AMBIGUOUS" : "NOT MATCHED", pal::InkFaint, Font::Label,
                    r.dlcMatch.ambiguous ? "More than one DLC display fits this one."
                                         : "No DLC display has this hardware id or runs what it runs.");
        return;
    }
    const char* by = r.dlcMatch.by == dlc::MatchBy::HardwareId ? "Matched by hardware id."
                     : r.dlcMatch.by == dlc::MatchBy::StackEvidence ? "Matched by the files DLC applied."
                                                                    : "Matched by a verification's hardware id.";
    MeasuredRow("DLC", r.dlcMatch.name, pal::Ink, Font::Label, by);
    if (rec.reminderDue) {
        std::string why = rec.correctionStale ? "Meter correction older than the profile allows." : "";
        if (g_recalReminderDays > 0 && rec.verifiedDaysAgo > g_recalReminderDays)
            why = "Last verified " + std::to_string(rec.verifiedDaysAgo) + " days ago." + (why.empty() ? "" : "\n" + why);
        MeasuredRow("DUE", "RECALIBRATE", pal::Alarm, Font::Label, why.c_str());
    }
    // A bypassed stage is what runs (nothing): the record no longer describes the screen.
    const MHCSettings& mhcShown = showHdr ? r.settings.hdrMHC : r.settings.sdrMHC;
    const bool mhcBypassed = mhcShown.enabled && (mhcShown.bypassPrimaries || mhcShown.bypassGrayscale);
    const bool cubeSet = !(showHdr ? r.settings.hdrPath : r.settings.sdrPath).empty();
    const bool cubeBypassed = (showHdr ? r.settings.hdrLutBypass : r.settings.sdrLutBypass) && cubeSet;
    // Suspended live layers, or an engine that should run and does not on the mode the display is in, take a
    // connected display's cube off the screen too (MHC stays): the record is read against what runs (TakeSnapshot).
    const bool cubeOff = cubeSet && !cubeBypassed && CubeOffScreen(snap, r, showHdr);
    const bool cubeSuspended = cubeOff && snap.suspended;
    const bool cubeDown = cubeOff && !snap.suspended;
    MeasuredRow("MHC", LayerText(rec.mhc, mhcBypassed), mhcBypassed ? pal::InkFaint : LayerColour(rec.mhc), Font::Label,
                mhcBypassed ? "A stage of the MHC profile is switched off." : nullptr);
    MeasuredRow("3D LUT", cubeSuspended ? "SUSPENDED" : cubeDown ? "NOT RUNNING" : LayerText(rec.cube, cubeBypassed),
                cubeDown ? pal::Alarm : (cubeBypassed || cubeSuspended ? pal::InkFaint : LayerColour(rec.cube)), Font::Label,
                cubeBypassed ? "The 3D LUT stage is switched off."
                : cubeSuspended ? "Live layers are suspended."
                : cubeDown ? "The engine is not running: the 3D LUT is not on screen." : nullptr);

    if (shown < 0) {
        MeasuredRow("VERIFIED", "NO REPORT", pal::InkFaint, Font::Label);
    } else {
        const dlc::Report& rep = d.reports[shown];
        const bool current = rec.current >= 0;
        const ImU32 val = current ? pal::Ink : pal::InkFaint;
        const int age = dlc::DaysBetween(rep.date, d.today);
        MeasuredRow(current ? "VERIFIED" : "LATEST", DateAge(rep.date, age), current ? pal::Ink : pal::Alarm, Font::Mono,
                    current ? nullptr : "Not a verification of what runs now: the newest report for this display and mode.");
        std::string flow = rep.flow;
        if (!rep.contentMode.empty() && rep.contentMode != rep.mode) flow += "  \xC2\xB7  " + rep.contentMode + " content";
        MeasuredRow("RUN", flow, pal::InkDim, Font::Mono);
        const dlc::Verification& v = rep.verification;
        MeasuredRow("METRIC", dlc::MetricLabel(v.metric), pal::InkDim, Font::Mono);
        std::vector<Readout> nums = {
            { "AVG", Num2(v.avg), val }, { "P95", Num2(v.p95), val }, { "MAX", Num2(v.max), val },
            { "WHITE", Num2(v.white), val }, { "GRAY", Num2(v.grayAvg), val },
        };
        if (v.patchCount > 0) nums.push_back({ "PATCHES", std::to_string(v.patchCount), pal::InkDim });
        Readouts(nums);
        if (!rep.runDir.empty()) {
            const auto patches = dlc::Patches(rep.runDir);
            if (!patches) MeasuredRow("", "LOADING", pal::InkFaint, Font::Label);
            else if (!patches->empty()) PatchHistogram(*patches);
        }
    }
    if (!rec.correctionMade.empty())
        MeasuredRow("METER", DateAge(rec.correctionMade, rec.correctionDaysAgo), rec.correctionStale ? pal::Alarm : pal::InkDim,
                    Font::Mono, rec.correctionStale ? "Meter correction older than the profile's max_age_days." : nullptr);
}

// The stage switch in the inspector's header: whether the stage runs. False = the stage has none (the
// shader tweak). Switching off keeps the stage's settings; the 3D LUT and MHC switches wait while DLC
// calibrates (as Suspend does).
bool StageSwitchState(const Snapshot& snap, const DisplayRow& r, const Stage& st, bool hdr, PanelSwitch& sw) {
    const bool parked = r.liveIndex < 0;
    const MHCSettings& mhc = hdr ? r.settings.hdrMHC : r.settings.sdrMHC;
    const bool mhcInstalled = mhc.enabled && !mhc.profileName.empty();
    switch (st.id) {
    case StageId::ToneMap:
        sw.on = r.settings.hdrColorCorrection.tonemap.enabled;
        sw.enabled = !parked;
        sw.tip = "Tone mapping on or off.";
        return true;
    case StageId::Lut3D: {
        const bool cube = !(hdr ? r.settings.hdrPath : r.settings.sdrPath).empty();
        sw.on = cube && !(hdr ? r.settings.hdrLutBypass : r.settings.sdrLutBypass);
        sw.enabled = cube && !parked && !snap.calibrating;
        sw.tip = !cube ? "No cube loaded." : snap.calibrating ? "Calibration in progress."
                                           : "Off keeps the cube loaded and applies nothing.";
        return true;
    }
    case StageId::Fald: {
        const FaldSettings& f = hdr ? r.settings.hdrColorCorrection.fald : r.settings.sdrColorCorrection.fald;
        sw.on = f.enabled;
        sw.enabled = !parked;
        sw.tip = st.light == Light::Fault && r.mode == Mode::SDR ? "FALD cannot run in legacy SDR (ACM or HDR only)."
                                                                 : "FALD correction on or off.";
        return true;
    }
    case StageId::Primaries:
    case StageId::Grayscale: {
        const bool primaries = st.id == StageId::Primaries;
        const bool content = primaries ? MhcPrimariesContent(mhc) : MhcGrayscaleContent(mhc, hdr);
        sw.on = mhcInstalled && content && !(primaries ? mhc.bypassPrimaries : mhc.bypassGrayscale);
        sw.enabled = mhcInstalled && content && !parked && !snap.calibrating;
        sw.tip = !mhcInstalled ? "No MHC profile installed." : !content ? "Nothing set in this stage."
                 : snap.calibrating ? "Calibration in progress."
                 : primaries ? "Off swaps in the profile without primaries and white balance. Settings stay."
                             : "Off swaps in the profile without its grayscale and gamma. Settings stay.";
        return true;
    }
    default:
        return false;
    }
}

void ApplyStageSwitch(const DisplayRow& r, const Stage& st, bool hdr, bool on) {
    switch (st.id) {
    case StageId::ToneMap: {
        TonemapSettings t = r.settings.hdrColorCorrection.tonemap;
        t.enabled = on;
        PostTo(r, [t](int mon) { act::SetTonemap(mon, t); });
        break;
    }
    case StageId::Lut3D:
        PostTo(r, [hdr, on](int mon) { act::SetLutBypass(mon, hdr, !on); });
        break;
    case StageId::Fald:
        PostTo(r, [hdr, on](int mon) { act::SetFaldEnabled(mon, hdr, on); });
        break;
    case StageId::Primaries:
        PostTo(r, [hdr, on](int mon) { act::MhcSetStageBypass(mon, hdr, act::MhcStage::Primaries, !on); });
        break;
    case StageId::Grayscale:
        PostTo(r, [hdr, on](int mon) { act::MhcSetStageBypass(mon, hdr, act::MhcStage::Grayscale, !on); });
        break;
    default:
        break;
    }
}

void DrawPipelinePage(const Snapshot& snap, ViewState& vs) {
    const DisplayRow* sel = FindSelected(snap, vs);
    const ImVec2 body(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float bodyH = FooterTop() - body.y;   // down to the pinned footer
    const float sideW = Snap(Px(kSidebarW));
    ImDrawList* dl = ImGui::GetWindowDrawList();

    if (Shape().sunkenSidebar) {
        // The display list in a sunken white list box.
        const float in = Snap(Px(8)), bi = 2.0f * BevelPx();
        const ImVec2 a(body.x + in, body.y + in), b(body.x + sideW - Snap(Px(2)), body.y + bodyH - in);
        dl->AddRectFilled(a, b, Col(pal::Field));
        Bevel(dl, a, b, BevelKind::Sunken);
        ImGui::SetCursorScreenPos(ImVec2(a.x + bi, a.y + bi));
        ImGui::BeginChild("##sidebar", ImVec2(b.x - a.x - 2 * bi, b.y - a.y - 2 * bi), ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar);
    } else {
        ImGui::SetCursorScreenPos(body);
        ImGui::BeginChild("##sidebar", ImVec2(sideW, bodyH), ImGuiChildFlags_None, ImGuiWindowFlags_NoScrollbar);
    }
    ImGui::BeginDisabled(EditorOpen(vs));   // the editor holds its display until DONE / CANCEL
    DrawSidebar(snap, vs, sel);
    ImGui::EndDisabled();
    ImGui::EndChild();
    if (!Shape().sunkenSidebar)
        dl->AddRectFilled(ImVec2(body.x + sideW - 1, body.y), ImVec2(body.x + sideW, body.y + bodyH), Col(pal::Rule));

    ImGui::SetCursorScreenPos(ImVec2(body.x + sideW, body.y));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(kGutter), Px(18)));
    ImGui::BeginChild("##main", ImVec2(0, bodyH), ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PopStyleVar();
    if (!sel) {
        BeginPanel("##none", "NO DISPLAYS", nullptr, ImVec2(0, 0));
        Text(Font::Label, kSizeLabel, pal::InkFaint, "NOTHING CONNECTED OR REMEMBERED");
        EndPanel();
        ImGui::EndChild();
        return;
    }
    const bool liveHdr = PipelineIsHdr(sel->mode);
    const bool showHdr = vs.showOtherMode ? !liveHdr : liveHdr;
    const Mode shown = showHdr ? Mode::HDR : (sel->mode == Mode::AcmSdr ? Mode::AcmSdr : Mode::SDR);

    // Title row: name left, mode switch right, both centred on one line. The "other mode" note is drawn
    // on this line without taking layout space, so nothing moves when it appears.
    {
        const ImVec2 p(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
        const float w = ImGui::GetContentRegionAvail().x;
        const float h = Snap(Px(30)), cy = p.y + h * 0.5f;
        const std::string name = Utf8(sel->name);
        const float nameW = DrawText(ImGui::GetWindowDrawList(), Font::BodyBold, kSizeTitle, p.x, cy, pal::Ink, name.c_str());
        if (vs.showOtherMode && sel->liveIndex >= 0)
            TextAt(Font::Label, kSizeLabel, pal::Alarm, p.x + nameW + Px(16), cy, liveHdr ? "DISPLAY IS IN HDR" : "DISPLAY IS IN SDR");
        // Which side's settings are shown; a green light marks the mode the display is in now.
        const bool parked = sel->liveIndex < 0;
        static const char* const kModes[] = { "HDR", "SDR" };
        const ImU32 dots[2] = { !parked && liveHdr ? pal::Good : 0u, !parked && !liveHdr ? pal::Good : 0u };
        const float segW = 72.0f;
        ImGui::SetCursorScreenPos(ImVec2(Snap(p.x + w - Snap(Px(segW)) * 2), Snap(cy - Snap(Px(kControlH)) * 0.5f)));
        ImGui::BeginDisabled(EditorOpen(vs));
        const int pick = Segmented("##mode", kModes, 2, showHdr ? 0 : 1, segW, dots);
        ImGui::EndDisabled();
        if (pick == 0) vs.showOtherMode = !liveHdr;
        else if (pick == 1) vs.showOtherMode = liveHdr;
        const ImVec2 ga = ImGui::GetItemRectMin(), gb = ImGui::GetItemRectMax();
        if (ImGui::IsMouseHoveringRect(ga, gb) && ImGui::IsWindowHovered()) {
            const bool overHdr = ImGui::GetIO().MousePos.x < (ga.x + gb.x) * 0.5f;
            const char* label = overHdr ? "HDR" : "SDR";
            if (parked) ImGui::SetTooltip("%s settings. Display not connected.", label);
            else ImGui::SetTooltip("%s settings. The display is in %s now.", label, liveHdr ? "HDR" : "SDR");
        }
        ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h));
        ImGui::Dummy(ImVec2(0, Px(16)));
    }

    PathRuntime selRt = RuntimeOf(snap);
    selRt.editHeld = sel->liveIndex >= 0 && MhcPreviewHolds(sel->liveIndex, PipelineIsHdr(shown));
    const std::vector<Stage> path = BuildPixelPath(sel->settings, shown, sel->mode, selRt);
    const Stage* st = nullptr;
    for (const Stage& s : path) if (s.id == vs.stage) st = &s;
    if (!st) {
        for (const Stage& s : path) if (s.selectable) { vs.stage = s.id; st = &s; break; }
    }
    ImGui::BeginDisabled(EditorOpen(vs));
    DrawPixelPath(path, vs);
    ImGui::EndDisabled();
    ImGui::Dummy(ImVec2(0, Px(20)));

    // An editor, while open, takes the place of both panels.
    if (vs.gsEdit.open) {
        DrawGsEditor(snap, vs, ImVec2(ImGui::GetContentRegionAvail().x, ImGui::GetContentRegionAvail().y));
        ImGui::EndChild();
        return;
    }
    if (vs.mhcEdit.open) {
        DrawMhcEditor(snap, vs, ImVec2(ImGui::GetContentRegionAvail().x, ImGui::GetContentRegionAvail().y));
        ImGui::EndChild();
        return;
    }

    // The selected node's panel, and the measured panel beside it; in a narrow window the measured panel
    // goes under it (both fit their content and the column scrolls).
    const ImVec2 top(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float total = ImGui::GetContentRegionAvail().x, colH = ImGui::GetContentRegionAvail().y;
    const float measuredW = Snap(Px(kMeasuredW)), gutter = Snap(Px(kGutter));
    const bool stacked = total - measuredW - gutter < Px(kInspectorMinW);
    const float inspW = stacked ? total : total - measuredW - gutter;
    PanelSwitch sw;
    const bool hasSwitch = st && StageSwitchState(snap, *sel, *st, showHdr, sw);
    BeginPanel("##inspector", st ? InspectorTitle(st->id) : "SELECT A STAGE", st ? &st->light : nullptr,
               ImVec2(inspW, stacked ? 0.0f : colH), hasSwitch ? &sw : nullptr);
    if (st) {
        ImGui::BeginDisabled(sel->liveIndex < 0);   // a remembered display that is not connected: view only
        // Widget ids per display and mode: a field being typed in stays with its display and mode (it ends
        // if either changes under it) instead of committing to whatever is shown next.
        ImGui::PushID(Utf8(RowIdOf(*sel)).c_str());
        ImGui::PushID(showHdr ? 1 : 0);
        DrawInspector(snap, *sel, *st, showHdr, vs);
        ImGui::PopID();
        ImGui::PopID();
        ImGui::EndDisabled();
    }
    EndPanel();
    if (hasSwitch && sw.clicked) ApplyStageSwitch(*sel, *st, showHdr, !sw.on);
    if (stacked) ImGui::Dummy(ImVec2(0, Px(kPanelGap)));
    else ImGui::SetCursorScreenPos(ImVec2(top.x + inspW + gutter, top.y));
    const Light measured = MeasuredLight(*sel, showHdr);
    BeginPanel("##measured", "MEASURED", &measured, stacked ? ImVec2(total, 0.0f) : ImVec2(total - inspW - gutter, colH));
    DrawMeasured(snap, *sel, showHdr, vs);
    EndPanel();
    ImGui::EndChild();
}

// ---------------------------------------------------------------------------------------------
// Settings

void SettingsGeneral() {
    RowCtx row = BeginRow("INTERFACE");
    RowTip(row, "Classic: the previous window. The tray switches too.");
    RowPlace(row);
    static const char* const kUi[] = { "NEW", "CLASSIC" };
    const int pick = Segmented("##set_ui", kUi, 2, g_uiClassic ? 1 : 0, 80);
    if (pick >= 0) {
        const bool classic = pick == 1;
        Post([classic]() { SetInterfaceClassic(classic); });
    }
    EndRow(row);

    row = BeginRow("THEME");
    RowTip(row, "Dark: Instrument. Gray: softer greys. Light: Windows 98 colours.");
    RowPlace(row);
    static const char* const kThemes[] = { "DARK", "GRAY", "LIGHT" };
    const int theme = Segmented("##set_theme", kThemes, 3, (int)CurrentTheme(), 64);
    if (theme >= 0) {
        Post([theme]() {
            act::SetUiTheme(theme);
            ApplyUiTheme();
        });
    }
    EndRow(row);

    row = BeginRow("RUN AT STARTUP");
    RowPlace(row);
    const int startup = StartupEnabledCached();
    bool v = false;
    if (startup < 0) RowText(row, "\xE2\x80\xA6", pal::InkFaint);
    else if (OnOff("##set_startup", startup == 1, &v)) Post([v]() { act::SetRunAtStartup(v); });
    EndRow(row);

    row = BeginRow("START MINIMIZED");
    RowTip(row, "Start in the tray, no window.");
    RowPlace(row);
    if (OnOff("##set_min", g_startMinimized.load(), &v)) Post([v]() { act::SetStartMinimized(v); });
    EndRow(row);
}

void SettingsEngine(const Snapshot& snap, ViewState& vs) {
    bool v = false;
    RowCtx row = BeginRow("DWM HOOK");
    RowTip(row, "3D LUT and tone mapping inside DWM: no overlay window.\nOff: the overlay applies them.");
    RowPlace(row);
    if (OnOff("##set_hook", snap.hookMode, &v)) Post([v]() { act::SetDwmHookMode(v); });
    EndRow(row);

    row = BeginRow("INTERPOLATION");
    RowTip(row, snap.hookMode ? "The DWM hook always interpolates tetrahedrally.\nTrilinear is an overlay-mode option."
                              : "3D LUT interpolation of the overlay.");
    RowPlace(row);
    static const char* const kInterp[] = { "TRILINEAR", "TETRAHEDRAL" };
    ImGui::BeginDisabled(snap.hookMode);   // fixed in hook mode: show what the hook does
    const int pick = Segmented("##set_interp", kInterp, 2, snap.hookMode || g_tetrahedralInterp.load() ? 1 : 0, 96);
    ImGui::EndDisabled();
    if (pick >= 0 && !snap.hookMode) {
        const bool tetra = pick == 1;
        Post([tetra]() { act::SetTetrahedral(tetra); });
    }
    EndRow(row);

    row = BeginRow("HDR DITHER");
    RowTip(row, "Dither the HDR output (overlay and DWM hook).");
    RowPlace(row);
    if (OnOff("##set_dither", g_hdrDither.load(), &v)) Post([v]() { act::SetHdrDither(v); });
    EndRow(row);

    auto listText = [](const std::wstring& raw) { return raw.empty() ? std::string("\xE2\x80\x94") : Utf8(raw); };
    row = BeginRow("PASSTHROUGH");
    RowTip(row, "Hide the overlay while a whitelisted app runs (keeps G-Sync).");
    if (RowButtons(row, "set_vrr", { "WHITELIST" }) == 0) OpenWhitelist(vs, 2);
    RowPlace(row);
    if (OnOff("##set_vrr_on", g_vrrWhitelistEnabled.load(), &v)) Post([v]() { act::SetVrrWhitelistEnabled(v); });
    RowNext(row);
    RowText(row, listText(g_vrrWhitelistRaw), g_vrrWhitelistRaw.empty() ? pal::InkFaint : pal::InkDim);
    EndRow(row);

    row = BeginRow("DESKTOP GAMMA");
    RowTip(row, "Desktop gamma turns off while a whitelisted app runs.");
    if (RowButtons(row, "set_gwl", { "WHITELIST" }) == 0) OpenWhitelist(vs, 1);
    RowText(row, listText(g_gammaWhitelistRaw), g_gammaWhitelistRaw.empty() ? pal::InkFaint : pal::InkDim);
    EndRow(row);
}

void SettingsHotkeys() {
    struct Hotkey { const char* label; const char* id; int hotkey; const std::atomic<bool>* on; const std::atomic<char>* key; };
    const Hotkey keys[] = {
        { "DESKTOP GAMMA", "##hk_gamma", HOTKEY_GAMMA, &g_hotkeyGammaEnabled, &g_hotkeyGammaKey },
        { "HDR", "##hk_hdr", HOTKEY_HDR_TOGGLE, &g_hotkeyHdrEnabled, &g_hotkeyHdrKey },
        { "ANALYSIS", "##hk_analysis", HOTKEY_ANALYSIS, &g_hotkeyAnalysisEnabled, &g_hotkeyAnalysisKey },
    };
    for (const Hotkey& k : keys) {
        RowCtx row = BeginRow(k.label);
        RowPlace(row);
        bool v = false;
        if (OnOff(k.id, k.on->load(), &v)) {
            const int id = k.hotkey;
            Post([id, v]() { act::SetHotkeyEnabled(id, v); });
        }
        RowNext(row);
        char combo[32];
        snprintf(combo, sizeof(combo), "WIN+SHIFT+%c", k.key->load());
        RowText(row, combo, k.on->load() ? pal::InkDim : pal::InkFaint);
        EndRow(row);
    }
}

// Folder picker (Shell dialog). Runs inside a posted command: the dialog pumps messages.
bool PickFolder(HWND owner, std::wstring& path) {
    IFileOpenDialog* dlg = nullptr;
    if (FAILED(CoCreateInstance(CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&dlg)))) return false;
    DWORD opts = 0;
    dlg->GetOptions(&opts);
    dlg->SetOptions(opts | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST);
    if (!path.empty()) {
        IShellItem* start = nullptr;
        if (SUCCEEDED(SHCreateItemFromParsingName(path.c_str(), nullptr, IID_PPV_ARGS(&start)))) {
            dlg->SetFolder(start);
            start->Release();
        }
    }
    bool ok = false;
    if (SUCCEEDED(dlg->Show(owner))) {
        IShellItem* item = nullptr;
        if (SUCCEEDED(dlg->GetResult(&item))) {
            PWSTR p = nullptr;
            if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &p))) {
                path = p;
                CoTaskMemFree(p);
                ok = true;
            }
            item->Release();
        }
    }
    dlg->Release();
    return ok;
}

void SettingsCalibration(const Snapshot& snap) {
    RowCtx row = BeginRow("DLC CONTROL");
    RowTip(row, "Lets DLC drive DesktopLUT over a local pipe while it calibrates.");
    RowPlace(row);
    bool v = false;
    if (OnOff("##set_calib", g_calibrationControlEnabled.load(), &v)) Post([v]() { ApplyCalibrationControl(v); });
    EndRow(row);

    row = BeginRow("DLC FOLDER");
    RowTip(row, "Where DLC keeps its profile, runs and results.\nEmpty: found next to DesktopLUT when it is there.");
    const int b = RowButtons(row, "set_dlc", { "BROWSE", "CLEAR" });
    if (b == 0) {
        Post([]() {
            std::wstring path = DlcFolder();
            if (!PickFolder(Window(), path)) return;
            act::SetDlcFolder(path);
            RequestDlcScan();
        });
    } else if (b == 1) {
        Post([]() {
            act::SetDlcFolder(L"");
            RequestDlcScan();
        });
    }
    const std::wstring& folder = snap.dlc->folder;
    std::string shown = folder.empty() ? std::string("\xE2\x80\x94") : Utf8(folder);
    if (g_dlcFolder.empty() && !folder.empty()) shown = "AUTO  " + shown;
    RowText(row, shown, folder.empty() ? pal::InkFaint : pal::Ink, Font::Mono, kSizeMono, folder);
    EndRow(row);

    row = BeginRow("REMINDER");
    RowTip(row, "Mark a display for recalibration when its last verification is older than this.\n"
                "A stale meter correction marks it too.");
    RowPlace(row);
    static const char* const kDays[] = { "OFF", "30 D", "90 D", "180 D" };
    static const int kDayValues[] = { 0, 30, 90, 180 };
    int cur = -1;
    for (int k = 0; k < 4; k++) if (g_recalReminderDays == kDayValues[k]) cur = k;
    const int pick = Segmented("##set_remind", kDays, 4, cur, 56);
    if (pick >= 0) {
        const int days = kDayValues[pick];
        Post([days]() { act::SetRecalReminderDays(days); });
    }
    EndRow(row);
}

void SettingsDiagnostics() {
    RowCtx row = BeginRow("CONSOLE LOG");
    RowTip(row, "A console window with the log.");
    RowPlace(row);
    bool v = false;
    if (OnOff("##set_console", g_consoleEnabled.load(), &v)) Post([v]() { act::SetConsoleLog(v); });
    EndRow(row);
}

void SettingsAbout(ViewState& vs) {
    RowCtx row = BeginRow("VERSION");
    RowText(row, DESKTOPLUT_VERSION_STR);
    EndRow(row);
    row = BeginRow("LICENSE");
    RowText(row, "GPL-3.0");
    EndRow(row);
    row = BeginRow("NOTICES");
    if (RowButtons(row, "set_notices", { vs.showNotices ? "HIDE" : "SHOW" }) == 0) vs.showNotices = !vs.showNotices;
    std::string names;
    for (int i = 0; i < kNoticeCount; i++) names += (i ? ", " : "") + std::string(kNotices[i].name);
    RowText(row, names, pal::InkDim, Font::Body, 13.0f);
    EndRow(row);
    if (!vs.showNotices) return;
    for (int i = 0; i < kNoticeCount; i++) {
        ImGui::Dummy(ImVec2(0, Px(10)));
        const std::string title = std::string(kNotices[i].name) + "  \xC2\xB7  " + kNotices[i].license;
        Text(Font::Label, kSizeLabel, pal::Ink, title.c_str());
        ImGui::Dummy(ImVec2(0, Px(4)));
        Text(Font::Mono, 11.0f, pal::InkDim, kNotices[i].text);
    }
}

void DrawSettingsPage(const Snapshot& snap, ViewState& vs) {
    const float bodyH = FooterTop() - ImGui::GetCursorScreenPos().y;   // down to the pinned footer
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(kGutter), Px(18)));
    ImGui::BeginChild("##settings", ImVec2(0, bodyH), ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PopStyleVar();
    // Two equal columns of panels, a gutter between them.
    auto section = [](const char* id, const char* title, const std::function<void()>& body) {
        BeginPanel(id, title, nullptr, ImVec2(0, 0));
        body();
        EndPanel();
        ImGui::Dummy(ImVec2(0, Px(kPanelGap)));
    };
    if (ImGui::GetContentRegionAvail().x < Px(kSettingsTwoColW)) {
        section("##s_general", "GENERAL", [] { SettingsGeneral(); });
        section("##s_engine", "ENGINE", [&] { SettingsEngine(snap, vs); });
        section("##s_calib", "CALIBRATION", [&] { SettingsCalibration(snap); });
        section("##s_hotkeys", "HOTKEYS", [] { SettingsHotkeys(); });
        section("##s_diag", "DIAGNOSTICS", [] { SettingsDiagnostics(); });
        section("##s_about", "ABOUT", [&] { SettingsAbout(vs); });
        ImGui::EndChild();
        return;
    }
    ImGui::PushStyleVar(ImGuiStyleVar_CellPadding, ImVec2(0, 0));
    if (ImGui::BeginTable("##settings_cols", 3, ImGuiTableFlags_None)) {
        ImGui::TableSetupColumn("left", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableSetupColumn("gap", ImGuiTableColumnFlags_WidthFixed, Px(kGutter));
        ImGui::TableSetupColumn("right", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableNextRow();
        ImGui::TableSetColumnIndex(0);
        section("##s_general", "GENERAL", [] { SettingsGeneral(); });
        section("##s_engine", "ENGINE", [&] { SettingsEngine(snap, vs); });
        section("##s_diag", "DIAGNOSTICS", [] { SettingsDiagnostics(); });
        ImGui::TableSetColumnIndex(2);
        section("##s_calib", "CALIBRATION", [&] { SettingsCalibration(snap); });
        section("##s_hotkeys", "HOTKEYS", [] { SettingsHotkeys(); });
        section("##s_about", "ABOUT", [&] { SettingsAbout(vs); });
        ImGui::EndTable();
    }
    ImGui::PopStyleVar();
    ImGui::EndChild();
}

// ---------------------------------------------------------------------------------------------
// Events: the notification log, optionally interleaved with the diagnostic output (logcap), as a log
// viewer: oldest at the top, follows the newest while scrolled to the bottom.

struct LogRow {
    uint64_t key = 0;            // kind in the top bit, sequence below: stable across rebuilds
    uint64_t ticks = 0;          // local time as FILETIME ticks (merge order)
    char time[16] = {};
    const char* level = "";
    enum class Tone : unsigned char { Good, Alarm, Dim, Faint } tone = Tone::Dim;   // resolved per frame (themes change)
    std::string source, display, message, detail;
    std::string search;          // lower-case haystack for the text filter
};

struct LogCache {
    uint64_t eventSeq = ~0ull, lineSeq = ~0ull;
    std::string filterKey;
    std::vector<LogRow> rows;
    size_t lastCount = 0;
};

uint64_t Ticks(const SYSTEMTIME& st) {
    FILETIME ft;
    if (!SystemTimeToFileTime(&st, &ft)) return 0;
    return ((uint64_t)ft.dwHighDateTime << 32) | ft.dwLowDateTime;
}

std::string Lower(std::string s) {
    for (char& c : s) c = (char)tolower((unsigned char)c);
    return s;
}

// "[DWM Hook] Injected" -> source "DWM HOOK", message "Injected".
void SplitTag(const std::string& line, std::string& source, std::string& message) {
    size_t i = 0;
    while (i < line.size() && line[i] == ' ') i++;
    if (i < line.size() && line[i] == '[') {
        const size_t close = line.find(']', i);
        if (close != std::string::npos && close - i < 40) {
            source = line.substr(i + 1, close - i - 1);
            for (char& c : source) c = (char)toupper((unsigned char)c);
            size_t m = close + 1;
            while (m < line.size() && (line[m] == ' ' || line[m] == ':')) m++;
            message = line.substr(m);
            return;
        }
    }
    message = line.substr(i);
}

std::string LogLineText(const LogRow& r) {
    std::string s = std::string(r.time) + "  " + r.level + "  " + r.source;
    if (!r.display.empty()) s += "  [" + r.display + "]";
    s += "  " + r.message;
    if (!r.detail.empty()) s += "  \xE2\x80\x94  " + r.detail;
    return s;
}

void RebuildLog(LogCache& c, const Snapshot& snap, const ViewState& vs) {
    c.rows.clear();
    const std::vector<notify::Event> events = notify::Snapshot();
    for (const notify::Event& e : events) {
        if (e.seq <= vs.log.clearedEventSeq) continue;
        const bool warn = e.sev == notify::Sev::Warning || e.sev == notify::Sev::Error;
        if (vs.log.warningsOnly && !warn) continue;
        if (vs.log.display >= 0 && e.monitor != vs.log.display) continue;
        LogRow r;
        r.key = e.seq;
        r.ticks = Ticks(e.time);
        snprintf(r.time, sizeof(r.time), "%02u:%02u:%02u.%03u", e.time.wHour, e.time.wMinute, e.time.wSecond, e.time.wMilliseconds);
        switch (e.sev) {
        case notify::Sev::Success: r.level = "OK"; r.tone = LogRow::Tone::Good; break;
        case notify::Sev::Warning: r.level = "WARN"; r.tone = LogRow::Tone::Alarm; break;
        case notify::Sev::Error: r.level = "ERROR"; r.tone = LogRow::Tone::Alarm; break;
        default: r.level = "INFO"; r.tone = LogRow::Tone::Dim; break;
        }
        r.source = Utf8(e.source);
        for (char& ch : r.source) ch = (char)toupper((unsigned char)ch);
        if (e.monitor >= 0) {
            for (const DisplayRow& d : snap.live)
                if (d.liveIndex == e.monitor) r.display = Utf8(d.name);
            if (r.display.empty()) r.display = "MON " + std::to_string(e.monitor);
        }
        r.message = Utf8(e.title);
        r.detail = Utf8(e.detail);
        c.rows.push_back(std::move(r));
    }
    if (vs.log.allOutput && vs.log.display < 0) {
        for (const auto& lp : logcap::Snapshot()) {
            const logcap::Line& l = *lp;
            if (l.seq <= vs.log.clearedLineSeq) continue;
            if (vs.log.warningsOnly && !l.err) continue;
            LogRow r;
            r.key = (1ull << 63) | l.seq;
            r.ticks = Ticks(l.time);
            snprintf(r.time, sizeof(r.time), "%02u:%02u:%02u.%03u", l.time.wHour, l.time.wMinute, l.time.wSecond, l.time.wMilliseconds);
            r.level = l.err ? "ERR" : "LOG";
            r.tone = l.err ? LogRow::Tone::Dim : LogRow::Tone::Faint;
            SplitTag(l.text, r.source, r.message);
            c.rows.push_back(std::move(r));
        }
        std::stable_sort(c.rows.begin(), c.rows.end(), [](const LogRow& a, const LogRow& b) { return a.ticks < b.ticks; });
    }
    const std::string needle = Lower(vs.log.filter);
    if (!needle.empty()) {
        std::vector<LogRow> kept;
        for (LogRow& r : c.rows) {
            r.search = Lower(r.source + " " + r.display + " " + r.message + " " + r.detail);
            if (r.search.find(needle) != std::string::npos) kept.push_back(std::move(r));
        }
        c.rows.swap(kept);
    }
}

void DrawEventsPage(const Snapshot& snap, ViewState& vs) {
    static LogCache cache;
    const float bodyH = FooterTop() - ImGui::GetCursorScreenPos().y;   // down to the pinned footer
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(kGutter), Px(14)));
    ImGui::BeginChild("##events", ImVec2(0, bodyH), ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleVar();

    // Toolbar: what, how severe, which display, text filter; copy and clear at the right end.
    LogView& lv = vs.log;
    {
        const ImVec2 p(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
        const float w = ImGui::GetContentRegionAvail().x, h = Snap(Px(28));
        const float y = Snap(p.y + (h - Snap(Px(kControlH))) * 0.5f);
        float x = p.x;
        auto next = [&]() { x = ImGui::GetItemRectMax().x + Px(16); ImGui::SetCursorScreenPos(ImVec2(Snap(x), y)); };
        ImGui::SetCursorScreenPos(ImVec2(x, y));
        static const char* const kWhat[] = { "EVENTS", "ALL OUTPUT" };
        const int what = Segmented("##log_what", kWhat, 2, lv.allOutput ? 1 : 0, 92);
        if (what >= 0) lv.allOutput = what == 1;
        if (ImGui::IsMouseHoveringRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax()) && ImGui::IsWindowHovered())
            ImGui::SetTooltip("All output: the diagnostic log too (what the console shows).");
        next();
        static const char* const kSev[] = { "ALL", "WARNINGS" };
        const int sev = Segmented("##log_sev", kSev, 2, lv.warningsOnly ? 1 : 0, 72);
        if (sev >= 0) lv.warningsOnly = sev == 1;
        next();
        std::string current = "ALL DISPLAYS";
        for (const DisplayRow& d : snap.live) if (d.liveIndex == lv.display) current = Utf8(d.name);
        if (BeginDropdown("##log_disp", current.c_str(), 170)) {
            if (DropdownItem("ALL DISPLAYS", lv.display < 0)) lv.display = -1;
            for (const DisplayRow& d : snap.live)
                if (DropdownItem(Utf8(d.name).c_str(), lv.display == d.liveIndex)) lv.display = d.liveIndex;
            EndDropdown();
        }
        next();
        TextField("##log_filter", "FILTER", lv.filter, sizeof(lv.filter), 200);

        const float bw = 72.0f;
        ImGui::SetCursorScreenPos(ImVec2(Snap(p.x + w - Px(bw * 2 + 8)), y));
        if (Button("##log_copy", "COPY", bw)) {
            std::string all;
            for (const LogRow& r : cache.rows) all += LogLineText(r) + "\r\n";
            ImGui::SetClipboardText(all.c_str());
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Copy the rows shown.");
        ImGui::SameLine(0, Px(8));
        if (Button("##log_clear", "CLEAR", bw)) {
            lv.clearedEventSeq = notify::LastSeq();
            lv.clearedLineSeq = logcap::LastSeq();
        }
        ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h + Px(10)));
        ImGui::Dummy(ImVec2(0, 0));
    }

    const std::string filterKey = std::to_string(lv.allOutput) + std::to_string(lv.warningsOnly) + "|" +
                                  std::to_string(lv.display) + "|" + lv.filter + "|" + std::to_string(lv.clearedEventSeq) +
                                  "|" + std::to_string(lv.clearedLineSeq);
    const uint64_t es = notify::LastSeq(), ls = lv.allOutput ? logcap::LastSeq() : 0;
    if (es != cache.eventSeq || ls != cache.lineSeq || filterKey != cache.filterKey) {
        RebuildLog(cache, snap, vs);
        cache.eventSeq = es;
        cache.lineSeq = ls;
        cache.filterKey = filterKey;
    }

    // The log sits in a panel surface (Light: a sunken white list).
    const ImVec2 la(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const ImVec2 lb(la.x + ImGui::GetContentRegionAvail().x, la.y + ImGui::GetContentRegionAvail().y);
    const bool list = Shape().bevel;
    float inset = 1.0f;
    if (list) {
        ImGui::GetWindowDrawList()->AddRectFilled(la, lb, Col(pal::Field));
        Bevel(ImGui::GetWindowDrawList(), la, lb, BevelKind::Sunken);
        inset = 2.0f * BevelPx();
    } else {
        const float r = Snap(Px(Shape().panelRounding));
        ImGui::GetWindowDrawList()->AddRectFilled(la, lb, Col(pal::Panel), r);
        ImGui::GetWindowDrawList()->AddRect(la, lb, Col(pal::PanelEdge), r);
    }
    ImGui::SetCursorScreenPos(ImVec2(la.x + inset, la.y + inset));

    const ImGuiTableFlags flags = ImGuiTableFlags_ScrollY | ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingFixedFit |
                                  ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_Resizable;
    ImGui::PushStyleColor(ImGuiCol_TableRowBg, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_TableBorderLight, list ? pal::BevelLight : pal::Rule);
    ImGui::PushStyleVar(ImGuiStyleVar_CellPadding, ImVec2(Px(8), Px(3)));
    if (ImGui::BeginTable("##log", 5, flags, ImVec2(lb.x - la.x - 2 * inset, lb.y - la.y - 2 * inset))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("TIME", ImGuiTableColumnFlags_WidthFixed, Px(96));
        ImGui::TableSetupColumn("LEVEL", ImGuiTableColumnFlags_WidthFixed, Px(52));
        ImGui::TableSetupColumn("SOURCE", ImGuiTableColumnFlags_WidthFixed, Px(110));
        ImGui::TableSetupColumn("DISPLAY", ImGuiTableColumnFlags_WidthFixed, Px(120));
        ImGui::TableSetupColumn("MESSAGE", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableNextRow(ImGuiTableRowFlags_Headers);
        for (int col = 0; col < 5; col++) {
            ImGui::TableSetColumnIndex(col);
            Text(Font::Label, kSizeLabel, pal::InkFaint, ImGui::TableGetColumnName(col));
        }
        // Follow the newest while scrolled to the bottom (like a terminal); scrolling up stops it.
        const bool atBottom = ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - Px(2);
        ImGui::PushFont(GetFont(Font::Mono), 12.0f);
        ImGuiListClipper clip;
        clip.Begin((int)cache.rows.size());
        while (clip.Step()) {
            for (int i = clip.DisplayStart; i < clip.DisplayEnd; i++) {
                const LogRow& r = cache.rows[(size_t)i];
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                ImGui::PushID((void*)(uintptr_t)r.key);
                const bool selected = lv.selectedKey == r.key;
                if (ImGui::Selectable("##row", selected, ImGuiSelectableFlags_SpanAllColumns | ImGuiSelectableFlags_AllowOverlap))
                    lv.selectedKey = selected ? 0 : r.key;
                ImGui::PushStyleColor(ImGuiCol_PopupBg, pal::Menu);
                if (ImGui::BeginPopupContextItem("##rowmenu")) {
                    if (DropdownItem("COPY LINE", false)) ImGui::SetClipboardText(LogLineText(r).c_str());
                    ImGui::EndPopup();
                }
                ImGui::PopStyleColor();
                // On the selection fill (navy in Light) every cell takes the selection's ink.
                auto ink = [&](ImU32 c) { return selected ? pal::ListSelInk : c; };
                ImGui::SameLine(0, 0);
                ImGui::PushStyleColor(ImGuiCol_Text, ink(pal::InkFaint));
                ImGui::TextUnformatted(r.time);
                ImGui::PopStyleColor();
                ImGui::TableSetColumnIndex(1);
                const ImU32 levelCol = r.tone == LogRow::Tone::Good ? pal::Good : r.tone == LogRow::Tone::Alarm ? pal::Alarm
                                       : r.tone == LogRow::Tone::Dim ? pal::InkDim : pal::InkFaint;
                ImGui::PushStyleColor(ImGuiCol_Text, ink(levelCol));
                ImGui::TextUnformatted(r.level);
                ImGui::PopStyleColor();
                ImGui::TableSetColumnIndex(2);
                ImGui::PushStyleColor(ImGuiCol_Text, ink(pal::InkDim));
                ImGui::TextUnformatted(r.source.c_str());
                ImGui::TableSetColumnIndex(3);
                ImGui::TextUnformatted(r.display.c_str());
                ImGui::PopStyleColor();
                ImGui::TableSetColumnIndex(4);
                ImGui::PushStyleColor(ImGuiCol_Text, ink(pal::Ink));
                ImGui::TextUnformatted(r.message.c_str());
                ImGui::PopStyleColor();
                if (!r.detail.empty()) {
                    ImGui::SameLine(0, Px(10));
                    ImGui::PushStyleColor(ImGuiCol_Text, ink(pal::InkFaint));
                    ImGui::TextUnformatted(r.detail.c_str());
                    ImGui::PopStyleColor();
                }
                ImGui::PopID();
            }
        }
        ImGui::PopFont();
        if (cache.rows.empty()) {
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(4);
            Text(Font::Label, kSizeLabel, pal::InkFaint, "NOTHING TO SHOW");
        }
        if (atBottom && cache.rows.size() != cache.lastCount) ImGui::SetScrollHereY(1.0f);
        cache.lastCount = cache.rows.size();
        // Ctrl+C copies the selected row.
        if (lv.selectedKey && ImGui::IsWindowFocused(ImGuiFocusedFlags_ChildWindows) &&
            ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_C)) {
            for (const LogRow& r : cache.rows)
                if (r.key == lv.selectedKey) ImGui::SetClipboardText(LogLineText(r).c_str());
        }
        ImGui::EndTable();
    }
    ImGui::PopStyleVar();
    ImGui::PopStyleColor(2);
    ImGui::EndChild();
}

// A confirmation: the title, one line, the action (the default button) and CANCEL. In Light it is a
// Windows 98 dialog: a raised frame and a navy caption carrying the title.
// A modal in the interface's look (a panel; Windows 98: a raised window with a blue caption). True = open: draw the
// content, then ImGui::EndPopup().
bool BeginModal(const char* id, const char* title) {
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(vp->WorkPos.x + vp->WorkSize.x * 0.5f, vp->WorkPos.y + vp->WorkSize.y * 0.4f),
                            ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    const bool bevel = Shape().bevel;
    const float captionH = Snap(Px(22));
    ImGui::PushStyleColor(ImGuiCol_PopupBg, bevel ? pal::Ground : pal::Panel);
    ImGui::PushStyleColor(ImGuiCol_Border, bevel ? pal::BevelDark : pal::PanelEdge);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(kGutter), Px(18)));
    ImGui::PushStyleVar(ImGuiStyleVar_PopupBorderSize, bevel ? 0.0f : 1.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, Px(Shape().panelRounding));
    const bool open = ImGui::BeginPopupModal(id, nullptr,
                                             ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoMove);
    ImGui::PopStyleVar(3);
    ImGui::PopStyleColor(2);
    if (!open) return false;
    if (bevel) {
        const ImVec2 a = ImGui::GetWindowPos(), s = ImGui::GetWindowSize(), b(a.x + s.x, a.y + s.y);
        ImDrawList* dl = ImGui::GetWindowDrawList();
        dl->PushClipRect(a, b, false);
        Bevel(dl, a, b, BevelKind::Raised);
        const float t = BevelPx();
        const ImVec2 ca(a.x + 3 * t, a.y + 3 * t), cb(b.x - 3 * t, a.y + 3 * t + captionH);
        dl->AddRectFilledMultiColor(ca, cb, Col(pal::Strong), Col(IM_COL32(0x10, 0x84, 0xd0, 0xff)),
                                    Col(IM_COL32(0x10, 0x84, 0xd0, 0xff)), Col(pal::Strong));
        DrawText(dl, Font::Label, kSizeLabel, ca.x + Px(6), (ca.y + cb.y) * 0.5f, pal::StrongInk, title);
        dl->PopClipRect();
        ImGui::Dummy(ImVec2(MeasureInk(Font::Label, kSizeLabel, title).advance + Px(24), captionH - Px(6)));
    } else {
        Text(Font::Label, 13.0f, pal::Ink, title);
        ImGui::Dummy(ImVec2(0, Px(4)));
    }
    return true;
}

std::vector<std::wstring> SplitWhitelist(const std::wstring& raw) {
    std::vector<std::wstring> out;
    std::wstring item;
    auto flush = [&]() {
        const size_t a = item.find_first_not_of(L" \t"), b = item.find_last_not_of(L" \t");
        if (a != std::wstring::npos) out.push_back(item.substr(a, b - a + 1));
        item.clear();
    };
    for (wchar_t c : raw) {
        if (c == L',' || c == L';' || c == L'\n' || c == L'\r') flush();
        else item += c;
    }
    flush();
    return out;
}

std::wstring JoinWhitelist(const std::vector<std::wstring>& items) {
    std::wstring raw;
    for (const auto& s : items) { if (!raw.empty()) raw += L", "; raw += s; }
    return raw;
}

// Process names of the apps with a visible window now (the RUNNING list), sorted, without DesktopLUT itself.
std::vector<std::wstring> RunningAppNames() {
    struct Ctx { std::vector<std::wstring> names; DWORD self; } ctx{ {}, GetCurrentProcessId() };
    EnumWindows([](HWND h, LPARAM lp) -> BOOL {
        Ctx* c = (Ctx*)lp;
        if (!IsWindowVisible(h) || GetWindow(h, GW_OWNER) || GetWindowTextLengthW(h) == 0) return TRUE;
        DWORD pid = 0;
        GetWindowThreadProcessId(h, &pid);
        if (pid == c->self) return TRUE;
        HANDLE p = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
        if (!p) return TRUE;
        wchar_t path[MAX_PATH];
        DWORD n = MAX_PATH;
        if (QueryFullProcessImageNameW(p, 0, path, &n)) {
            std::wstring name(path, n);
            const size_t slash = name.find_last_of(L"\\/");
            if (slash != std::wstring::npos) name = name.substr(slash + 1);
            for (wchar_t& ch : name) ch = (wchar_t)towlower(ch);
            if (name != L"explorer.exe" && name != L"applicationframehost.exe" && name != L"textinputhost.exe")
                c->names.push_back(name);
        }
        CloseHandle(p);
        return TRUE;
    }, (LPARAM)&ctx);
    std::sort(ctx.names.begin(), ctx.names.end());
    ctx.names.erase(std::unique(ctx.names.begin(), ctx.names.end()), ctx.names.end());
    return ctx.names;
}

void DrawWhitelist(ViewState& vs) {
    if (vs.whitelistOpenRequested) {
        ImGui::OpenPopup("##whitelist");
        vs.whitelistOpenRequested = false;
    }
    const bool gamma = vs.whitelist == 1;
    if (!BeginModal("##whitelist", gamma ? "DESKTOP GAMMA WHITELIST" : "PASSTHROUGH WHITELIST")) return;
    const std::wstring& raw = gamma ? g_gammaWhitelistRaw : g_vrrWhitelistRaw;
    std::vector<std::wstring> items = SplitWhitelist(raw);
    auto apply = [gamma](std::vector<std::wstring> next) {
        const std::wstring joined = JoinWhitelist(next);
        Post([gamma, joined]() { if (gamma) act::SetGammaWhitelist(joined); else act::SetVrrWhitelist(joined); });
    };
    ImGui::Dummy(ImVec2(0, Px(6)));

    // The apps as chips, wrapped; the cross removes one.
    const float maxW = Px(460);
    const ImVec2 start(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    ImDrawList* dl = ImGui::GetWindowDrawList();
    float x = start.x, y = start.y;
    const float chipH = Snap(Px(kControlH));
    int remove = -1;
    for (size_t i = 0; i < items.size(); i++) {
        const std::string name = Utf8(items[i]);
        const float tw = MeasureInk(Font::Mono, 12.0f, name.c_str()).advance;
        const float w = Snap(tw + Px(12) + Px(22));
        if (x > start.x && x + w > start.x + maxW) { x = start.x; y += chipH + Px(6); }
        const ImVec2 a(x, y), b(x + w, y + chipH);
        DrawFieldBox(dl, a, b);
        DrawText(dl, Font::Mono, 12.0f, a.x + Px(8), (a.y + b.y) * 0.5f, pal::Ink, name.c_str());
        ImGui::SetCursorScreenPos(ImVec2(b.x - Px(22), a.y));
        ImGui::PushID((int)i);
        if (ImGui::InvisibleButton("##rm", ImVec2(Px(22), chipH))) remove = (int)i;
        const bool hot = ImGui::IsItemHovered();
        ImGui::PopID();
        const ImVec2 c(b.x - Px(11), (a.y + b.y) * 0.5f);
        const float r = Px(3.5f);
        const ImU32 xc = Col(hot ? pal::Ink : pal::InkFaint);
        dl->AddLine(ImVec2(c.x - r, c.y - r), ImVec2(c.x + r, c.y + r), xc, Px(1.3f));
        dl->AddLine(ImVec2(c.x - r, c.y + r), ImVec2(c.x + r, c.y - r), xc, Px(1.3f));
        if (hot) ImGui::SetTooltip("Remove");
        x += w + Px(6);
    }
    if (items.empty()) {
        DrawText(dl, Font::Label, kSizeLabel, start.x, start.y + chipH * 0.5f, pal::InkFaint, "NO APPS");
    }
    ImGui::SetCursorScreenPos(ImVec2(start.x, y + chipH + Px(14)));
    ImGui::Dummy(ImVec2(maxW, 0));
    if (remove >= 0) {
        items.erase(items.begin() + remove);
        apply(items);
    }

    // Add: typed, or picked from the apps running now.
    auto addName = [&](std::wstring name) {
        const size_t a = name.find_first_not_of(L" \t"), b = name.find_last_not_of(L" \t");
        if (a == std::wstring::npos) return;
        name = name.substr(a, b - a + 1);
        if (name.find(L'.') == std::wstring::npos) name += L".exe";
        for (const auto& s : items) if (_wcsicmp(s.c_str(), name.c_str()) == 0) return;
        items.push_back(name);
        apply(items);
    };
    if (vs.whitelistRefocus) {
        ImGui::SetKeyboardFocusHere();
        vs.whitelistRefocus = false;
    }
    bool enterKey = false;   // read inside the field (its box is the last item after it)
    TextField("##wl_add", "app.exe", vs.whitelistAdd, sizeof(vs.whitelistAdd), 220, &enterKey);
    ImGui::SameLine(0, Px(8));
    if (Button("##wl_addbtn", "ADD", 64) || enterKey) {
        addName(Wide(vs.whitelistAdd));
        vs.whitelistAdd[0] = 0;
        if (enterKey) vs.whitelistRefocus = true;
    }
    ImGui::SameLine(0, Px(8));
    static std::vector<std::wstring> s_running;
    if (BeginDropdown("##wl_running", "RUNNING", 150)) {
        if (ImGui::IsWindowAppearing()) s_running = RunningAppNames();
        if (s_running.empty()) Text(Font::Label, kSizeLabel, pal::InkFaint, "NONE");
        for (const auto& n : s_running)
            if (DropdownItem(Utf8(n).c_str(), false)) addName(n);
        EndDropdown();
    }

    ImGui::Dummy(ImVec2(0, Px(14)));
    if (PushButton("##wl_done", "DONE", kButtonW, 26, true) || ImGui::IsKeyPressed(ImGuiKey_Escape)) {
        vs.whitelist = 0;
        ImGui::CloseCurrentPopup();
    }
    ImGui::EndPopup();
}

void DrawConfirm(ViewState& vs) {
    if (vs.confirmOpenRequested) {
        ImGui::OpenPopup("##confirm");
        vs.confirmOpenRequested = false;
    }
    if (!BeginModal("##confirm", vs.confirm.title.c_str())) return;
    ImGui::TextUnformatted(vs.confirm.detail.c_str());
    ImGui::Dummy(ImVec2(0, Px(14)));
    if (PushButton("##confirm_yes", vs.confirm.action.c_str(), kButtonW, 26, true)) {
        if (vs.confirm.onConfirm) Post(vs.confirm.onConfirm);
        vs.confirm = {};
        ImGui::CloseCurrentPopup();
    }
    ImGui::SameLine(0, Px(8));
    if (PushButton("##confirm_no", "CANCEL", kButtonW, 26) || ImGui::IsKeyPressed(ImGuiKey_Escape)) {
        vs.confirm = {};
        ImGui::CloseCurrentPopup();
    }
    ImGui::EndPopup();
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

    // The editor drives arrow keys itself: ImGui's keyboard navigation would move focus with them.
    ImGuiIO& io = ImGui::GetIO();
    if (EditorOpen(vs)) io.ConfigFlags &= ~ImGuiConfigFlags_NavEnableKeyboard;
    else io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
    DrawHeader(snap, vs, busy || EditorOpen(vs));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(Px(8), Px(6)));
    ImGui::BeginDisabled(busy);   // an action is running (it may pump): nothing else may start meanwhile
    SetLogRedraws(vs.page == Page::Events && vs.log.allOutput);   // diagnostic lines redraw only here
    if (vs.page == Page::Events) DrawEventsPage(snap, vs);
    else if (vs.page == Page::Settings) DrawSettingsPage(snap, vs);
    else DrawPipelinePage(snap, vs);
    ImGui::EndDisabled();
    ImGui::PopStyleVar();
    DrawFooter(snap, vs, busy);
    DrawConfirm(vs);
    DrawWhitelist(vs);
    ImGui::End();
}

}  // namespace ui
