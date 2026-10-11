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

// The physical display (device path, else EDID id): follows it between live and remembered.
std::wstring KeyOf(const DisplayRow& r) {
    if (!r.settings.identity.devicePath.empty()) return r.settings.identity.devicePath;
    if (!r.settings.identity.edidId.empty()) return r.settings.identity.edidId;
    return L"#" + std::to_wstring(r.liveIndex);
}

// One sidebar row: unique even when a live display and a remembered one share a connector (a same-model
// replacement) or remembered entries have no identity.
std::wstring RowIdOf(const DisplayRow& r) {
    return (r.liveIndex >= 0 ? std::wstring(L"L|") : L"P" + std::to_wstring(r.settings.slot) + L"|") + KeyOf(r);
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

// The explanation of a row lives in a tooltip on its label (labels stay terse).
void RowTip(const RowCtx& r, const char* tip) {
    if (ImGui::IsMouseHoveringRect(ImVec2(r.x0, r.y0), ImVec2(r.x0 + Px(kRowLabelW) - Px(8), r.y0 + r.h)))
        ImGui::SetTooltip("%s", tip);
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
    float x = p.x + w - Px(kGutter) - Px(bw * 4 + gap * 2 + groupGap);
    ImGui::SetCursorScreenPos(ImVec2(x, cy - Px(bh) * 0.5f));
    ImGui::BeginDisabled(busy || (snap.calibrating && !snap.suspended));
    if (Toggle("##suspend", "SUSPEND", snap.suspended, ImVec2(bw, bh))) {
        const bool want = !snap.suspended;
        Post([want]() { act::SetLiveLayersSuspended(want); });
    }
    ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip(snap.calibrating && !snap.suspended ? "Calibration in progress."
                          : snap.suspended ? "3D LUT, tone mapping and FALD off. MHC stays on."
                                           : "Turn off 3D LUT, tone mapping and FALD. MHC stays on.");
    ImGui::SameLine(0, Px(groupGap));
    if (Toggle("##pg_pipe", "PIPELINE", vs.page == Page::Pipeline, ImVec2(bw, bh))) vs.page = Page::Pipeline;
    ImGui::SameLine(0, Px(gap));
    if (Toggle("##pg_events", "EVENTS", vs.page == Page::Events, ImVec2(bw, bh))) vs.page = Page::Events;
    ImGui::SameLine(0, Px(gap));
    if (Toggle("##pg_settings", "SETTINGS", vs.page == Page::Settings, ImVec2(bw, bh))) {
        if (vs.page != Page::Settings) Post([]() { RefreshStartupEnabledCache(); });   // COM: not in a frame
        vs.page = Page::Settings;
    }

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

void SidebarRow(const DisplayRow& r, bool selected, ViewState& vs) {
    const float w = ImGui::GetContentRegionAvail().x;
    const bool live = r.liveIndex >= 0;
    const float h = Px(live ? 60.0f : 44.0f);
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const std::wstring key = RowIdOf(r);
    const bool renaming = vs.renamingKey == key;
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
        if (r.reminderDue) {   // right-aligned under the mode badge: nothing else moves
            const char* due = "RECAL";
            const float dueW = MeasureText(Font::Label, kSizeLabel, due).x;
            TextAt(Font::Label, kSizeLabel, selected ? pal::Ground : pal::Alarm, p.x + w - pad - dueW, cy, due);
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
            RunOnDisplay(target.settings.identity, target.liveIndex, [hdr](int mon) { act::MhcResetCorrectionGs(mon, hdr); });
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
            RunOnDisplay(target.settings.identity, target.liveIndex,
                         [hdr, count](int mon) { act::MhcSetCorrectionGsPoints(mon, hdr, count); });
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
        RowTip(row, "SDR content in HDR as gamma 2.2 at the Windows SDR white level.\n"
                    "The whitelist turns it off while a listed app runs.");
        if (RowButtons(row, "dg_wl", { "WHITELIST" }) == 0) Post([]() { ShowGammaWhitelistDialog(Window()); });
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

// ---------------------------------------------------------------------------------------------
// Measured column: what DLC recorded and measured for the selected display and the mode shown.

constexpr float kMeasuredLabelW = 84.0f;

// A compact row of the measured column: label, value (ellipsized), optional tooltip on the row.
void MeasuredRow(const char* label, const std::string& value, ImU32 col = pal::Ink, Font f = Font::Mono,
                 const char* tip = nullptr) {
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float w = ImGui::GetContentRegionAvail().x, h = Px(24), cy = p.y + h * 0.5f;
    TextAt(Font::Label, kSizeLabel, pal::InkDim, p.x, cy, label);
    const float x = p.x + Px(kMeasuredLabelW);
    const float size = f == Font::Mono ? kSizeMono : kSizeLabel;
    const std::string shown = Ellipsize(f, size, value, p.x + w - x);
    TextAt(f, size, col, x, cy, shown.c_str());
    if (tip && ImGui::IsMouseHoveringRect(p, ImVec2(p.x + w, p.y + h))) ImGui::SetTooltip("%s", tip);
    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h));
    ImGui::Dummy(ImVec2(0, 0));
}

std::string Num2(double v) { return std::isfinite(v) ? Fmt("%.2f", v) : std::string("\xE2\x80\x94"); }

std::string DateAge(const std::string& date, int days) {
    if (date.empty()) return "\xE2\x80\x94";
    std::string s = date.substr(0, 10);
    if (days >= 0) s += "  \xC2\xB7  " + std::to_string(days) + " D";
    return s;
}

const char* LayerText(dlc::LayerState s) {
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
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float w = ImGui::GetContentRegionAvail().x, barH = Px(56), labelH = Px(16);
    const float gap = Px(4), bw = (w - gap * 5) / 6;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    for (int k = 0; k < 6; k++) {
        const float x = p.x + k * (bw + gap);
        const float h = barH * (float)bins[k] / (float)top;
        dl->AddRectFilled(ImVec2(x, p.y + barH - (h > 0 ? (h < Px(1) ? Px(1) : h) : 0)), ImVec2(x + bw, p.y + barH),
                          k >= 4 ? pal::Alarm : pal::InkDim);
        const ImVec2 ts = MeasureText(Font::Mono, 11.0f, kLabels[k]);
        dl->AddText(GetFont(Font::Mono), 11.0f * Dpi(), ImVec2(x + (bw - ts.x) * 0.5f, p.y + barH + Px(3)), pal::InkFaint, kLabels[k]);
        if (ImGui::IsMouseHoveringRect(ImVec2(x, p.y), ImVec2(x + bw, p.y + barH + labelH)))
            ImGui::SetTooltip("%d of %d patches", bins[k], n);
    }
    dl->AddLine(ImVec2(p.x, p.y + barH), ImVec2(p.x + w, p.y + barH), pal::Rule);
    ImGui::Dummy(ImVec2(w, barH + labelH + Px(4)));
}

void DrawMeasured(const Snapshot& snap, const DisplayRow& r, bool showHdr, ViewState& vs) {
    const dlc::Data& d = *snap.dlc;
    const int mi = showHdr ? 1 : 0;
    const dlc::ModeRecord& rec = r.dlcRecord[mi];
    const int shown = rec.current >= 0 ? rec.current : rec.latest;
    Light light = Light::Off;
    if (rec.current >= 0) light = rec.reminderDue ? Light::Fault : Light::Configured;
    else if (shown >= 0) light = Light::Fault;
    SectionHeader("MEASURED", &light);

    if (d.folder.empty()) {
        MeasuredRow("DLC", "NO FOLDER", pal::InkFaint, Font::Label, "No DLC folder set or found. Settings > Calibration.");
        ImGui::Dummy(ImVec2(0, Px(6)));
        if (Button("##meas_settings", "SETTINGS", kButtonW)) vs.page = Page::Settings;
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
    MeasuredRow("DLC", r.dlcMatch.name, pal::InkDim, Font::Label, by);
    if (rec.reminderDue) {
        std::string why = rec.correctionStale ? "Meter correction older than the profile allows." : "";
        if (g_recalReminderDays > 0 && rec.verifiedDaysAgo > g_recalReminderDays)
            why = "Last verified " + std::to_string(rec.verifiedDaysAgo) + " days ago." + (why.empty() ? "" : "\n" + why);
        MeasuredRow("DUE", "RECALIBRATE", pal::Alarm, Font::Label, why.c_str());
    }
    MeasuredRow("MHC", LayerText(rec.mhc), LayerColour(rec.mhc), Font::Label);
    MeasuredRow("3D LUT", LayerText(rec.cube), LayerColour(rec.cube), Font::Label);
    ImGui::Dummy(ImVec2(0, Px(8)));

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
        MeasuredRow("AVG", Num2(v.avg), val);
        MeasuredRow("P95", Num2(v.p95), val);
        MeasuredRow("MAX", Num2(v.max), val);
        MeasuredRow("WHITE", Num2(v.white), val);
        MeasuredRow("GRAY", Num2(v.grayAvg), val);
        if (v.patchCount > 0) MeasuredRow("PATCHES", std::to_string(v.patchCount), pal::InkDim);
        ImGui::Dummy(ImVec2(0, Px(8)));
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
        // Which side's settings are shown; a green light marks the mode the display is in now.
        ImGui::SetCursorScreenPos(ImVec2(p.x + w - Px(bw * 2), cy - Px(13)));
        auto modeButton = [&](const char* id, const char* label, bool isHdr) {
            const bool live = !parked && liveHdr == isHdr;
            const bool clicked = Toggle(id, label, showHdr == isHdr, ImVec2(bw, 26));
            const ImVec2 a = ImGui::GetItemRectMin(), b = ImGui::GetItemRectMax();
            if (live) ImGui::GetWindowDrawList()->AddCircleFilled(ImVec2(a.x + Px(10), (a.y + b.y) * 0.5f), Px(3), pal::Good);
            if (ImGui::IsItemHovered()) {
                if (parked) ImGui::SetTooltip("%s settings. Display not connected.", label);
                else if (live) ImGui::SetTooltip("%s settings. The display is in %s now.", label, label);
                else ImGui::SetTooltip("%s settings. The display is in %s now.", label, isHdr ? "SDR" : "HDR");
            }
            return clicked;
        };
        if (modeButton("##m_hdr", "HDR", true)) vs.showOtherMode = !liveHdr;
        ImGui::SameLine(0, 0);
        if (modeButton("##m_sdr", "SDR", false)) vs.showOtherMode = liveHdr;
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
    // Widget ids per display and mode: a field being typed in stays with its display and mode (it ends
    // if either changes under it) instead of committing to whatever is shown next.
    ImGui::PushID(Utf8(RowIdOf(*sel)).c_str());
    ImGui::PushID(showHdr ? 1 : 0);
    DrawInspector(snap, *sel, st, showHdr, vs);
    ImGui::PopID();
    ImGui::PopID();
    ImGui::EndDisabled();
    ImGui::EndChild();
    ImGui::SameLine(0, Px(kGutter));
    ImGui::BeginChild("##measured", ImVec2(0, colH), ImGuiChildFlags_None);
    DrawMeasured(snap, *sel, showHdr, vs);
    ImGui::EndChild();
    ImGui::EndChild();
}

// ---------------------------------------------------------------------------------------------
// Settings

void SettingsSpacer() { ImGui::Dummy(ImVec2(0, Px(18))); }

void SettingsGeneral() {
    SectionHeader("GENERAL", nullptr);
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

void SettingsEngine(const Snapshot& snap) {
    SectionHeader("ENGINE", nullptr);
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
    if (RowButtons(row, "set_vrr", { "WHITELIST" }) == 0) Post([]() { ShowVrrWhitelistDialog(Window()); });
    RowPlace(row);
    if (OnOff("##set_vrr_on", g_vrrWhitelistEnabled.load(), &v)) Post([v]() { act::SetVrrWhitelistEnabled(v); });
    RowNext(row);
    RowText(row, listText(g_vrrWhitelistRaw), g_vrrWhitelistRaw.empty() ? pal::InkFaint : pal::InkDim);
    EndRow(row);

    row = BeginRow("DESKTOP GAMMA");
    RowTip(row, "Desktop gamma turns off while a whitelisted app runs.");
    if (RowButtons(row, "set_gwl", { "WHITELIST" }) == 0) Post([]() { ShowGammaWhitelistDialog(Window()); });
    RowText(row, listText(g_gammaWhitelistRaw), g_gammaWhitelistRaw.empty() ? pal::InkFaint : pal::InkDim);
    EndRow(row);
}

void SettingsHotkeys() {
    SectionHeader("HOTKEYS", nullptr);
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
    SectionHeader("CALIBRATION", nullptr);
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
    SectionHeader("DIAGNOSTICS", nullptr);
    RowCtx row = BeginRow("CONSOLE LOG");
    RowTip(row, "A console window with the log.");
    RowPlace(row);
    bool v = false;
    if (OnOff("##set_console", g_consoleEnabled.load(), &v)) Post([v]() { act::SetConsoleLog(v); });
    EndRow(row);
}

void SettingsAbout(ViewState& vs) {
    SectionHeader("ABOUT", nullptr);
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
    // Two equal columns with a double gutter between them.
    ImGui::PushStyleVar(ImGuiStyleVar_CellPadding, ImVec2(0, 0));
    if (ImGui::BeginTable("##settings_cols", 3, ImGuiTableFlags_None)) {
        ImGui::TableSetupColumn("left", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableSetupColumn("gap", ImGuiTableColumnFlags_WidthFixed, Px(kGutter * 2));
        ImGui::TableSetupColumn("right", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableNextRow();
        ImGui::TableSetColumnIndex(0);
        SettingsGeneral();
        SettingsSpacer();
        SettingsEngine(snap);
        SettingsSpacer();
        SettingsHotkeys();
        ImGui::TableSetColumnIndex(2);
        SettingsCalibration(snap);
        SettingsSpacer();
        SettingsDiagnostics();
        SettingsSpacer();
        SettingsAbout(vs);
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
    ImU32 levelCol = 0;
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
        case notify::Sev::Success: r.level = "OK"; r.levelCol = pal::Good; break;
        case notify::Sev::Warning: r.level = "WARN"; r.levelCol = pal::Alarm; break;
        case notify::Sev::Error: r.level = "ERROR"; r.levelCol = pal::Alarm; break;
        default: r.level = "INFO"; r.levelCol = pal::InkDim; break;
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
        for (const logcap::Line& l : logcap::Snapshot()) {
            if (l.seq <= vs.log.clearedLineSeq) continue;
            if (vs.log.warningsOnly && !l.err) continue;
            LogRow r;
            r.key = (1ull << 63) | l.seq;
            r.ticks = Ticks(l.time);
            snprintf(r.time, sizeof(r.time), "%02u:%02u:%02u.%03u", l.time.wHour, l.time.wMinute, l.time.wSecond, l.time.wMilliseconds);
            r.level = l.err ? "ERR" : "LOG";
            r.levelCol = l.err ? pal::InkDim : pal::InkFaint;
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
        const ImVec2 p = ImGui::GetCursorScreenPos();
        const float w = ImGui::GetContentRegionAvail().x, h = Px(28), cy = p.y + h * 0.5f;
        ImGui::SetCursorScreenPos(ImVec2(p.x, cy - Px(12)));
        static const char* const kWhat[] = { "EVENTS", "ALL OUTPUT" };
        const int what = Segmented("##log_what", kWhat, 2, lv.allOutput ? 1 : 0, 92);
        if (what >= 0) lv.allOutput = what == 1;
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("All output: the diagnostic log too (what the console shows).");
        ImGui::SameLine(0, Px(16));
        static const char* const kSev[] = { "ALL", "WARNINGS" };
        const int sev = Segmented("##log_sev", kSev, 2, lv.warningsOnly ? 1 : 0, 72);
        if (sev >= 0) lv.warningsOnly = sev == 1;
        ImGui::SameLine(0, Px(16));
        std::string current = "ALL DISPLAYS";
        for (const DisplayRow& d : snap.live) if (d.liveIndex == lv.display) current = Utf8(d.name);
        ImGui::SetNextItemWidth(Px(170));
        if (ImGui::BeginCombo("##log_disp", current.c_str())) {
            if (ImGui::Selectable("ALL DISPLAYS", lv.display < 0)) lv.display = -1;
            for (const DisplayRow& d : snap.live)
                if (ImGui::Selectable(Utf8(d.name).c_str(), lv.display == d.liveIndex)) lv.display = d.liveIndex;
            ImGui::EndCombo();
        }
        ImGui::SameLine(0, Px(16));
        ImGui::SetNextItemWidth(Px(200));
        ImGui::InputTextWithHint("##log_filter", "FILTER", lv.filter, sizeof(lv.filter));

        const float bw = 72.0f;
        ImGui::SetCursorScreenPos(ImVec2(p.x + w - Px(bw * 2 + 8), cy - Px(12)));
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

    const ImGuiTableFlags flags = ImGuiTableFlags_ScrollY | ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingFixedFit |
                                  ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_Resizable;
    ImGui::PushStyleColor(ImGuiCol_TableRowBg, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_TableRowBgAlt, IM_COL32(255, 255, 255, 6));
    ImGui::PushStyleColor(ImGuiCol_TableBorderLight, pal::Rule);
    ImGui::PushStyleVar(ImGuiStyleVar_CellPadding, ImVec2(Px(8), Px(3)));
    if (ImGui::BeginTable("##log", 5, flags, ImVec2(0, ImGui::GetContentRegionAvail().y))) {
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
                if (ImGui::BeginPopupContextItem("##rowmenu")) {
                    if (ImGui::Selectable("COPY LINE")) ImGui::SetClipboardText(LogLineText(r).c_str());
                    ImGui::EndPopup();
                }
                ImGui::SameLine(0, 0);
                ImGui::PushStyleColor(ImGuiCol_Text, pal::InkFaint);
                ImGui::TextUnformatted(r.time);
                ImGui::PopStyleColor();
                ImGui::TableSetColumnIndex(1);
                ImGui::PushStyleColor(ImGuiCol_Text, r.levelCol);
                ImGui::TextUnformatted(r.level);
                ImGui::PopStyleColor();
                ImGui::TableSetColumnIndex(2);
                ImGui::PushStyleColor(ImGuiCol_Text, pal::InkDim);
                ImGui::TextUnformatted(r.source.c_str());
                ImGui::TableSetColumnIndex(3);
                ImGui::TextUnformatted(r.display.c_str());
                ImGui::PopStyleColor();
                ImGui::TableSetColumnIndex(4);
                ImGui::TextUnformatted(r.message.c_str());
                if (!r.detail.empty()) {
                    ImGui::SameLine(0, Px(10));
                    ImGui::PushStyleColor(ImGuiCol_Text, pal::InkFaint);
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
    ImGui::PopStyleColor(3);
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
    SetLogRedraws(vs.page == Page::Events && vs.log.allOutput);   // diagnostic lines redraw only here
    if (vs.page == Page::Events) DrawEventsPage(snap, vs);
    else if (vs.page == Page::Settings) DrawSettingsPage(snap, vs);
    else DrawPipelinePage(snap, vs);
    ImGui::EndDisabled();
    ImGui::PopStyleVar();
    DrawFooter(snap, busy);
    DrawConfirm(vs);
    ImGui::End();
}

}  // namespace ui
