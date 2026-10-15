#include "ui_dlc.h"

#include "ui_charts.h"
#include "ui_kit.h"
#include "ui_model.h"
#include "ui_window.h"
#include "actions/notify.h"
#include "dlc/dlc_scan.h"
#include "globals.h"
#include <shellapi.h>
#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdio>

namespace ui {

namespace {

constexpr float kStripH = 40.0f;       // the run strip
constexpr float kStepsH = 30.0f;       // the stepper / the record picker
constexpr float kReadoutsH = 60.0f;    // the readouts row
constexpr float kTileHeadH = kControlH;
constexpr float kFeedRowH = 40.0f;
constexpr float kRunPickerW = 400.0f;   // the record picker (DIPs)

const dlc::Live& RunOf(const Snapshot& s) {
    static const dlc::Live kNone;
    return s.run ? *s.run : kNone;
}

bool InProgress(dlc::RunPhase p) {
    return p == dlc::RunPhase::Running || p == dlc::RunPhase::Paused || p == dlc::RunPhase::Stalled ||
           p == dlc::RunPhase::Waiting || p == dlc::RunPhase::NoData;
}

std::string Upper(std::string s) {
    for (char& c : s) c = (char)std::toupper((unsigned char)c);
    for (char& c : s) if (c == '_') c = ' ';
    return s;
}

std::string Dur(double s) {
    if (!std::isfinite(s) || s < 0) return "\xE2\x80\x94";
    char b[32];
    if (s < 90) snprintf(b, sizeof(b), "%d S", (int)std::lround(s));
    else if (s < 3600) snprintf(b, sizeof(b), "%d MIN", (int)std::lround(s / 60.0));
    else snprintf(b, sizeof(b), "%d H %02d", (int)(s / 3600.0), (int)std::fmod(s / 60.0, 60.0));
    return b;
}

std::string Hhmm(const std::string& iso) {   // "2026-10-14T22:39:55.304" -> "22:39"
    return iso.size() >= 16 ? iso.substr(11, 5) : iso;
}

const char* MetricName(const std::string& key, bool hdr) {
    if (key == "itp" || key == "dE_ITP") return "\xCE\x94" "E ITP";
    if (key == "de2000" || key == "CIEDE2000") return "\xCE\x94" "E2000";
    return hdr ? "\xCE\x94" "E ITP" : "\xCE\x94" "E2000";
}

// How the run's state reads: its word, light and colour.
struct Look { std::string word; Light light; ImU32 col; };
Look LookOf(const dlc::Live& L) {
    using P = dlc::RunPhase;
    switch (L.phase) {
    case P::Running:
        if (L.state && L.state->light == "slow") return { "SLOW", Light::Pending, pal::Alarm };
        return { "LIVE", Light::Configured, pal::Ink };
    case P::Paused:  return { "PAUSED", Light::Pending, pal::Ink };
    case P::Waiting: return { "WAITING", Light::Pending, pal::Ink };
    case P::Stalled: return { "STALLED", Light::Fault, pal::Alarm };
    case P::NoData:  return { "NO DATA", Light::Fault, pal::Alarm };
    case P::Failed:  return { "FAILED", Light::Fault, pal::Alarm };
    case P::Done: {
        const std::string st = L.state ? L.state->runStatus : L.active.state;
        if (st == "completed" || st == "done") return { "COMPLETED", Light::Configured, pal::Ink };
        if (st.rfind("revert_", 0) == 0 || st == "reverted_partially") return { Upper(st), Light::Fault, pal::Alarm };
        return { st.empty() ? std::string("DONE") : Upper(st), Light::Off, pal::InkDim };
    }
    default: return { "NO RUN", Light::Off, pal::InkFaint };
    }
}

const DisplayRow* RunRow(const Snapshot& snap) {
    for (const DisplayRow& r : snap.live) if (r.dlcRun) return &r;
    return nullptr;
}

std::string DriverTag(const dlc::ActivePointer& a) {
    if (a.driver == "claude" || a.driver == "llm") return "CLAUDE";
    if (a.driver == "desktoplut") return "YOU";
    if (a.driver.empty()) return std::string();
    return Upper(a.driver);
}

void BoxFill(ImDrawList* dl, ImVec2 a, ImVec2 b) {
    if (Shape().bevel) {
        dl->AddRectFilled(a, b, Col(pal::Panel));
        Bevel(dl, a, b, BevelKind::Etched);
    } else {
        dl->AddRectFilled(a, b, Col(pal::Panel), Snap(Px(Shape().panelRounding)));
        dl->AddRect(a, b, Col(pal::PanelEdge), Snap(Px(Shape().panelRounding)));
    }
}

void OpenFolderSelecting(const std::wstring& path) {
    if (path.empty()) return;
    // explorer.exe hands the window to the user's own (unelevated) shell: nothing opens elevated.
    const std::wstring args = L"/select,\"" + path + L"\"";
    ShellExecuteW(nullptr, L"open", L"explorer.exe", args.c_str(), nullptr, SW_SHOWNORMAL);
}

bool FileExists(const std::wstring& p) {
    const DWORD a = GetFileAttributesW(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

void Control(const std::wstring& runDir, dlc::RunControl action) {
    Post([runDir, action]() {
        std::string err;
        if (!dlc::WriteRunControl(runDir, action, &err))
            notify::Post(notify::Sev::Warning, -1, L"dlc", L"Could not reach the run", Wide(err.c_str()));
        else
            notify::Post(notify::Sev::Info, -1, L"dlc",
                         action == dlc::RunControl::Pause ? L"Pause requested" :
                         action == dlc::RunControl::Resume ? L"Resume requested" : L"Cancel requested");
    });
}

// Moves the interface to the middle of another display's work area (off the one being measured).
void MoveWindowOff(HMONITOR measured, HMONITOR to) {
    HWND h = Window();
    if (!h || !to || to == measured) return;
    MONITORINFO mi = { sizeof(mi) };
    if (!GetMonitorInfo(to, &mi)) return;
    if (IsZoomed(h)) ShowWindow(h, SW_RESTORE);
    RECT wr;
    GetWindowRect(h, &wr);
    const RECT& wa = mi.rcWork;
    const int w = std::min<int>(wr.right - wr.left, wa.right - wa.left), hgt = std::min<int>(wr.bottom - wr.top, wa.bottom - wa.top);
    SetWindowPos(h, nullptr, wa.left + (wa.right - wa.left - w) / 2, wa.top + (wa.bottom - wa.top - hgt) / 2, w, hgt,
                 SWP_NOZORDER | SWP_NOACTIVATE);
}

// ---- the run strip: the run's state, where it is, PAUSE / RESUME / CANCEL ---------------------------------------
void RunStrip(const Snapshot& snap, ViewState& vs, const DisplayRow* sel) {
    const dlc::Live& L = RunOf(snap);
    const dlc::LiveState* st = L.state.get();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 a(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float w = ImGui::GetContentRegionAvail().x, h = Snap(Px(kStripH));
    const ImVec2 b(a.x + w, a.y + h);
    const float cy = Snap(a.y + h * 0.5f);
    BoxFill(dl, a, b);

    // Buttons first (right end, always there: nothing moves when they enable).
    const bool canPause = L.phase == dlc::RunPhase::Running || L.phase == dlc::RunPhase::Stalled;
    const bool paused = L.phase == dlc::RunPhase::Paused;
    const bool canCancel = InProgress(L.phase) && !L.cancelRequested;
    const std::wstring runDir = !L.active.run.empty() ? L.active.run : (st ? st->runDir : std::wstring());
    const float bw = Px(kButtonW), gap = Px(8);
    float bx = Snap(b.x - Px(12) - bw * 2 - gap);
    const float right = bx - Px(12);
    ImGui::SetCursorScreenPos(ImVec2(bx, Snap(cy - Px(kControlH) * 0.5f)));
    ImGui::BeginDisabled(!(canPause || paused) || runDir.empty());
    if (Button("##dlc_pause", paused ? "RESUME" : "PAUSE", kButtonW))
        Control(runDir, paused ? dlc::RunControl::Resume : dlc::RunControl::Pause);
    ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip(paused ? "Continue the run." : "Hold the run. After 3 minutes DLC rolls it back.");
    ImGui::SetCursorScreenPos(ImVec2(bx + bw + gap, Snap(cy - Px(kControlH) * 0.5f)));
    ImGui::BeginDisabled(!canCancel || runDir.empty());
    if (Button("##dlc_cancel", "CANCEL", kButtonW))
        AskConfirm(vs, "CANCEL RUN?", "DLC stops and restores the setup from before the run.", "CANCEL RUN",
                   [runDir]() {
                       std::string err;
                       if (!dlc::WriteRunControl(runDir, dlc::RunControl::Cancel, &err))
                           notify::Post(notify::Sev::Warning, -1, L"dlc", L"Could not reach the run", Wide(err.c_str()));
                       else
                           notify::Post(notify::Sev::Info, -1, L"dlc", L"Cancel requested");
                   });
    ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip(L.cancelRequested ? "Cancel requested: DLC stops at its next checkpoint."
                          : L.phase == dlc::RunPhase::Waiting ? "Stops the run: its next resume restores the setup."
                                                              : "Stop the run and restore the setup from before it.");

    // State: light + word.
    const Look look = LookOf(L);
    float x = Snap(a.x + Px(14));
    const float d = Snap(Px(8));
    DrawLight(dl, ImVec2(x + d * 0.5f, cy), look.light, d);
    x += d + Px(8);
    std::string word = look.word;
    if (paused && !L.pauseRequestedIso.empty()) word = "PAUSED";
    if (L.cancelRequested && InProgress(L.phase)) word = "CANCELLING";
    TextAt(Font::Label, kSizeLabel, look.col, x, cy, word.c_str());
    x = Snap(a.x + Px(14 + 8 + 8 + 92));

    if (L.phase == dlc::RunPhase::None || !st) {
        const char* what = L.phase == dlc::RunPhase::None ? "" : (L.phase == dlc::RunPhase::NoData ? "NO LIVE DATA FROM THE RUN" : "");
        TextAt(Font::Label, kSizeLabel, pal::InkFaint, x, cy, what);
        ImGui::SetCursorScreenPos(ImVec2(a.x, b.y));
        ImGui::Dummy(ImVec2(w, 0));
        return;
    }
    // What runs where (the display only when another one is shown: a click on it shows the run's).
    const DisplayRow* rr = RunRow(snap);
    std::string who = rr ? Utf8(rr->name) : st->display;
    if (who.empty()) who = "DISPLAY " + std::to_string(st->monitor);
    const bool elsewhere = !rr || rr != sel;
    const std::string what = (elsewhere ? who + "  \xC2\xB7  " : std::string()) + Upper(st->flow) + "  \xC2\xB7  " + st->mode;
    const float avail = right - x;
    const std::string driver = DriverTag(L.active);
    const float etaW = Px(84), countW = Px(78);
    const bool roomy = avail > Px(480);
    const float whatW = std::min(MeasureText(Font::Label, kSizeLabel, Upper(what).c_str()).x + Px(24), avail * (roomy ? 0.30f : 0.40f));
    const std::string whatShown = Ellipsize(Font::Label, kSizeLabel, Upper(what), whatW - Px(24));
    const float ww = DrawText(dl, Font::Label, kSizeLabel, x, cy, pal::InkDim, whatShown.c_str());
    if (ImGui::IsMouseHoveringRect(ImVec2(x, a.y), ImVec2(x + ww, b.y)) && ImGui::IsWindowHovered()) {
        const std::string tip = who + "  \xC2\xB7  " + st->flow + "  \xC2\xB7  " + st->mode + (st->target.empty() ? "" : "\n" + st->target);
        ImGui::SetTooltip("%s", tip.c_str());
        if (rr && elsewhere && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            vs.selectedKey = RowIdOf(*rr);
            vs.showOtherMode = (st->mode == "HDR") != PipelineIsHdr(rr->mode);
            vs.dlc.pick.clear();
        }
    }
    x += whatW;
    // Stage k/N.
    std::string stage;
    for (const dlc::LiveStep& s : st->steps) if (s.status == dlc::StepStatus::Current) stage = Upper(s.label);
    if (stage.empty()) stage = Upper(st->stage);
    char kn[32] = "";
    if (st->stepIndex > 0 && st->stepTotal > 0) snprintf(kn, sizeof(kn), "%d/%d  ", st->stepIndex, st->stepTotal);
    const std::string stageText = std::string(kn) + stage;
    const float stageW = std::min(MeasureText(Font::Label, kSizeLabel, stageText.c_str()).x + Px(24), avail * (roomy ? 0.32f : 0.45f));
    TextAt(Font::Label, kSizeLabel, pal::Ink, x, cy, Ellipsize(Font::Label, kSizeLabel, stageText, stageW - Px(24)).c_str());
    x += stageW;
    // From the right: the driver tag, ETA, the count; the bar (this stage's patches) takes what is left.
    float rx = right;
    if (roomy && !driver.empty()) {
        const InkBox ib = MeasureInk(Font::Label, 11.0f, driver.c_str());
        const ImVec2 ta(Snap(rx - ib.advance - Px(12)), Snap(cy - Px(9))), tb(Snap(rx), Snap(cy + Px(9)));
        dl->AddRect(ta, tb, Col(pal::RuleStrong));
        DrawTextIn(dl, Font::Label, 11.0f, ta, tb, pal::Ink, driver.c_str());
        if (ImGui::IsMouseHoveringRect(ta, tb) && ImGui::IsWindowHovered())
            ImGui::SetTooltip(driver == "CLAUDE" ? "Claude drives this run and decides its seams." : "Who started the run.");
        rx = ta.x - Px(14);
    }
    if (roomy) {
        rx -= etaW;
        const std::string eta = L.phase == dlc::RunPhase::Running && std::isfinite(st->etaS) ? "ETA " + Dur(st->etaS) : std::string();
        TextAt(Font::Mono, 12.0f, pal::InkDim, rx, cy, eta.c_str());
        if (!eta.empty() && ImGui::IsMouseHoveringRect(ImVec2(rx, a.y), ImVec2(rx + etaW, b.y)) && ImGui::IsWindowHovered())
            ImGui::SetTooltip("This stage. Up to %s at the slowest reads so far.\nRun time %s.", Dur(st->etaHiS).c_str(),
                              Dur(st->runElapsedS).c_str());
    }
    rx -= countW;
    char count[32] = "";
    if (st->patchesTotal > 0) snprintf(count, sizeof(count), "%d/%d", st->patchesDone, st->patchesTotal);
    TextAt(Font::Mono, kSizeMono, pal::Ink, rx, cy, count);
    const float barEnd = rx - Px(14);
    if (barEnd - x > Px(40)) {
        const float bh = Snap(Px(4));
        const ImVec2 ba(Snap(x), Snap(cy - bh * 0.5f)), bb(Snap(barEnd), Snap(cy - bh * 0.5f) + bh);
        if (Shape().bevel) {   // no recessed track in Light: a Windows 98 progress bar, the bar inset in a shallow border
            const float in = BevelPx() + Snap(Px(1));
            Bevel(dl, ImVec2(ba.x - in, ba.y - in), ImVec2(bb.x + in, bb.y + in), BevelKind::Shallow);
        } else {
            dl->AddRectFilled(ba, bb, Col(pal::Track));
        }
        if (st->patchesTotal > 0) {
            const float f = std::clamp((float)st->patchesDone / (float)st->patchesTotal, 0.0f, 1.0f);
            dl->AddRectFilled(ba, ImVec2(Snap(ba.x + (bb.x - ba.x) * f), bb.y), Col(pal::Ink));
        }
    }
    ImGui::SetCursorScreenPos(ImVec2(a.x, b.y));
    ImGui::Dummy(ImVec2(w, 0));
}

// A stage name cut at its end to fit `maxW` pixels (the label font), an ellipsis after it.
std::string EllipsizeEnd(const std::string& s, float maxW) {
    if (MeasureText(Font::Label, kSizeLabel, s.c_str()).x <= maxW) return s;
    const std::string dots = "\xE2\x80\xA6";
    for (size_t cut = s.size(); cut > 0; cut--) {
        if ((s[cut] & 0xC0) == 0x80) continue;   // a UTF-8 continuation byte: not a code point start
        std::string t = s.substr(0, cut);
        while (!t.empty() && t.back() == ' ') t.pop_back();
        t += dots;
        if (MeasureText(Font::Label, kSizeLabel, t.c_str()).x <= maxW) return t;
    }
    return dots;
}

// ---- the stepper: the run's planned stages, done / current / still to come ---------------------------------------
void Stepper(const dlc::LiveState& st, const Look& look) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 a(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float w = ImGui::GetContentRegionAvail().x, h = Snap(Px(kStepsH)), cy = Snap(a.y + h * 0.5f);
    const size_t n = st.steps.size();
    if (n > 0) {
        // Every label in full when they fit; else the current one in full and the others shortened to equal cells;
        // else only dots, the current one labelled. A shortened or hidden label shows on hover.
        std::vector<std::string> labels(n), shown(n);
        std::vector<float> lw(n), widths(n);
        const float d = Snap(Px(6)), pad = Px(10), minGap = Px(14);
        const float chrome = d + Px(6) + 2 * pad;   // a cell without its text
        float need = 0.0f;
        int cur = -1;
        for (size_t i = 0; i < n; i++) {
            labels[i] = Upper(st.steps[i].label);
            lw[i] = MeasureText(Font::Label, kSizeLabel, labels[i].c_str()).x + chrome;
            need += lw[i];
            if (st.steps[i].status == dlc::StepStatus::Current) cur = (int)i;
        }
        const float gaps = minGap * (float)(n - 1);
        if (need + gaps <= w) {
            widths = lw;
            shown = labels;
        } else {
            const float curW = cur >= 0 ? lw[(size_t)cur] : 0.0f;
            const size_t others = n - (cur >= 0 ? 1 : 0);
            const float cell = others ? (w - curW - gaps) / (float)others : 0.0f;
            const bool shorten = cell - chrome >= MeasureText(Font::Label, kSizeLabel, "ABC\xE2\x80\xA6").x;
            for (size_t i = 0; i < n; i++) {
                if ((int)i == cur) { widths[i] = lw[i]; shown[i] = labels[i]; continue; }
                widths[i] = shorten ? std::min(cell, lw[i]) : d + 2 * pad;
                if (shorten) shown[i] = EllipsizeEnd(labels[i], widths[i] - chrome + Px(2));
            }
        }
        float total = 0.0f;
        for (float wi : widths) total += wi;
        const float gapW = n > 1 ? std::max(0.0f, (w - total) / (float)(n - 1)) : 0.0f;
        float x = a.x;
        for (size_t i = 0; i < n; i++) {
            const dlc::LiveStep& s = st.steps[i];
            const bool cur = s.status == dlc::StepStatus::Current;
            const bool done = s.status == dlc::StepStatus::Done;
            const ImVec2 na(Snap(x), Snap(cy - Px(11))), nb(Snap(x + widths[i]), Snap(cy + Px(11)));
            // The current stage: a highlight, not a control (Light: flat like a list selection, no bevel).
            if (cur)
                dl->AddRectFilled(na, nb, Col(look.light == Light::Fault ? pal::Alarm : pal::Strong), Snap(Px(Shape().controlRounding)));
            const ImU32 ink = cur ? (look.light == Light::Fault ? pal::Ground : pal::StrongInk) : (done ? pal::InkDim : pal::InkFaint);
            float lx = na.x + pad;
            DrawLight(dl, ImVec2(lx + d * 0.5f, cy), done || cur ? Light::Configured : Light::Off, d, cur ? ink : 0);
            lx += d + Px(6);
            if (!shown[i].empty()) TextAt(Font::Label, kSizeLabel, ink, lx, cy, shown[i].c_str());
            if (shown[i] != labels[i] && ImGui::IsMouseHoveringRect(na, nb) && ImGui::IsWindowHovered()) ImGui::SetTooltip("%s", labels[i].c_str());
            if (i + 1 < n) {
                const float lx0 = nb.x + Px(6), lx1 = nb.x + gapW - Px(6);
                if (lx1 > lx0) dl->AddRectFilled(ImVec2(Snap(lx0), cy), ImVec2(Snap(lx1), cy + 1), Col(pal::Rule));
            }
            x += widths[i] + gapW;
        }
    }
    ImGui::SetCursorScreenPos(ImVec2(a.x, a.y + h));
    ImGui::Dummy(ImVec2(w, 0));
}

// ---- a row of label-over-value readouts, equal columns ----------------------------------------------------------
// `weight`: the tile's share of the row; `valueSize`: its number's size (DIPs).
struct Tile {
    std::string label, value, sub;
    ImU32 col;
    uint32_t swatch = 0;
    float weight = 1.0f, valueSize = 18.0f;
};
void ReadoutRow(const std::vector<Tile>& tiles) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 a(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float w = ImGui::GetContentRegionAvail().x, h = Snap(Px(kReadoutsH));
    BoxFill(dl, a, ImVec2(a.x + w, a.y + h));
    float units = 0.0f;
    for (const Tile& t : tiles) units += t.weight;
    if (units > 0.0f) {
        const float unitW = (w - Px(28)) / units;
        float x0 = a.x + Px(14);
        for (const Tile& t : tiles) {
            const float colW = unitW * t.weight;
            float x = Snap(x0);
            if (t.swatch) {
                const float s = Snap(Px(30));
                const ImVec2 sa(x, Snap(a.y + (h - s) * 0.5f));
                dl->AddRectFilled(sa, ImVec2(sa.x + s, sa.y + s), IM_COL32((t.swatch >> 16) & 0xFF, (t.swatch >> 8) & 0xFF, t.swatch & 0xFF, 255));
                dl->AddRect(sa, ImVec2(sa.x + s, sa.y + s), Col(pal::RuleStrong));
                x += s + Px(10);
            }
            const float maxW = colW - (x - x0) - Px(8);
            TextAt(Font::Label, 11.0f, pal::InkDim, x, a.y + h * 0.24f, Ellipsize(Font::Label, 11.0f, t.label, maxW).c_str());
            TextAt(Font::Mono, t.valueSize, t.col, x, a.y + h * 0.52f, Ellipsize(Font::Mono, t.valueSize, t.value, maxW).c_str());
            if (!t.sub.empty()) TextAt(Font::Mono, 11.0f, pal::InkFaint, x, a.y + h * 0.80f, Ellipsize(Font::Mono, 11.0f, t.sub, maxW).c_str());
            x0 += colW;
        }
    }
    ImGui::SetCursorScreenPos(ImVec2(a.x, a.y + h));
    ImGui::Dummy(ImVec2(w, 0));
}

std::string N2(double v) { return Num2(v); }

std::vector<Tile> LiveTiles(const dlc::LiveState& st, const dlc::LiveCharts* charts) {
    std::vector<Tile> t;
    const dlc::LiveRead& r = st.lastRead;
    const char* metric = MetricName(st.liveDe.metric, st.hdr);
    std::string sig = "\xE2\x80\x94";
    if (r.present && std::isfinite(r.signal[0])) sig = Fmt("%.2f ", r.signal[0]) + Fmt("%.2f ", r.signal[1]) + Fmt("%.2f", r.signal[2]);
    std::string lum = std::isfinite(r.Y) ? Fmt(r.Y < 10 ? "%.3f" : "%.1f", r.Y) : std::string("\xE2\x80\x94");
    if (std::isfinite(r.tY)) lum += " / " + Fmt(r.tY < 10 ? "%.3f" : "%.1f", r.tY);
    t.push_back({ r.label.empty() ? std::string("PATCH") : "PATCH " + Upper(r.label), sig, lum + " CD/M\xC2\xB2",
                  !r.present ? pal::InkFaint : (r.ok ? pal::Ink : pal::Alarm), r.present ? r.swatch : 0u, 2.0f, 14.0f });
    t.push_back({ metric, N2(r.de), r.neutral && std::isfinite(r.cct) ? Fmt("%.0f K", r.cct) : std::string(),
                  std::isfinite(r.de) && r.de > 3.0 ? pal::Alarm : pal::Ink });
    // This stage's reads; once a stage without reads follows (the scoring), the scored pass instead.
    const bool live = st.liveDe.n > 0 || !st.scored.present;
    const double avg = live ? st.liveDe.avg : st.scored.avg, mx = live ? st.liveDe.max : st.scored.max;
    t.push_back({ "AVG", N2(avg), live ? (st.liveDe.n > 0 ? std::to_string(st.liveDe.n) + " PATCHES" : std::string()) : std::string("SCORED"),
                  pal::Ink });
    // Weighted by where content lives (the practical view), the max beside it: a max is a trophy, not a verdict.
    const dlc::Practical* pr = charts && charts->present && charts->practical.present ? &charts->practical : nullptr;
    if (pr) t.push_back({ "SCORE", N2(pr->score), "MAX " + N2(mx), pal::Ink });
    else t.push_back({ "MAX", N2(mx), std::string(), pal::Ink });
    char reads[48];
    snprintf(reads, sizeof(reads), "%d", st.readsOk);
    t.push_back({ "READS", reads, st.readsFailed > 0 ? std::to_string(st.readsFailed) + " FAILED" : std::string(),
                  st.readsFailed > 0 ? pal::Alarm : pal::Ink });
    if (st.scored.present)
        t.push_back({ "SCORED", N2(st.scored.avg), "P95 " + N2(st.scored.p95) + "  MAX " + N2(st.scored.max), pal::Ink });
    else
        t.push_back({ "STAGE", Dur(st.stageElapsedS), "RUN " + Dur(st.runElapsedS), pal::Ink });
    return t;
}

std::vector<Tile> RecordTiles(const dlc::Report* rep) {
    if (!rep) {   // the same tiles, empty: nothing moves when a record arrives
        const std::string dash = "\xE2\x80\x94";
        return { { "AVG", dash, std::string(), pal::InkFaint }, { "P95", dash, std::string(), pal::InkFaint },
                 { "MAX", dash, std::string(), pal::InkFaint }, { "WHITE", dash, std::string(), pal::InkFaint },
                 { "GRAY", dash, std::string(), pal::InkFaint }, { "PATCHES", dash, std::string(), pal::InkFaint } };
    }
    const dlc::Verification& v = rep->verification;
    const std::string metric = dlc::MetricLabel(v.metric);
    return {
        { metric + " AVG", N2(v.avg), std::string(), pal::Ink },
        { "P95", N2(v.p95), std::string(), pal::Ink },
        { "MAX", N2(v.max), std::string(), pal::Ink },
        { "WHITE", N2(v.white), std::string(), pal::Ink },
        { "GRAY", N2(v.grayAvg), std::string(), pal::Ink },
        { "PATCHES", v.patchCount > 0 ? std::to_string(v.patchCount) : std::string("\xE2\x80\x94"), std::string(), pal::InkDim },
    };
}

std::string NitsText(float v) {
    if (!std::isfinite(v)) return "\xE2\x80\x94";
    return Fmt(v >= 100.0f ? "%.0f" : v >= 1.0f ? "%.1f" : v >= 0.1f ? "%.2f" : v >= 0.01f ? "%.3f" : "%.4f", v) + " NIT";
}

// A record's readouts from its practical view: what a person acts on — the content-weighted score, the worst miss
// that matters, the white, the panel's reach. The report's own p95 / max stay beside them.
std::vector<Tile> PracticalTiles(const dlc::Practical& pr, const dlc::Report* rep, bool hdr) {
    std::vector<Tile> t;
    const bool scored = std::isfinite(pr.contentScore);
    t.push_back({ "SCORE", N2(scored ? pr.contentScore : pr.score), pr.contentWeighted ? "CONTENT" : "SURVEY", pal::Ink, 0u, 0.9f });
    t.push_back({ "P95", rep ? N2(rep->verification.p95) : std::string("\xE2\x80\x94"),
                  rep ? "MAX " + N2(rep->verification.max) : std::string(), pal::Ink, 0u, 0.9f });
    if (!pr.worst.empty()) {
        const dlc::WorstRow& w = pr.worst.front();
        t.push_back({ "WORST", N2(w.de), w.desc, w.de > 3.0f ? pal::Alarm : pal::Ink, w.intended, 1.6f });
    } else {
        t.push_back({ "WORST", "\xE2\x80\x94", std::string(), pal::InkFaint, 0u, 1.6f });
    }
    const dlc::PanelLimits& l = pr.limits;
    if (l.hasWhite) {
        std::string sub = NitsText(l.whiteY);
        if (std::isfinite(l.whiteTargetY)) sub = Fmt(l.whiteY >= 100.0f ? "%.0f" : "%.1f", l.whiteY) + " / " + NitsText(l.whiteTargetY);
        if (std::isfinite(l.whiteDe)) sub += "  \xC2\xB7  " + N2(l.whiteDe);
        t.push_back({ hdr ? "WHITE 203" : "WHITE", std::isfinite(l.whiteCct) ? Fmt("%.0f K", l.whiteCct) : std::string("\xE2\x80\x94"), sub, pal::Ink,
                      0u, 1.3f });
    } else {
        t.push_back({ hdr ? "WHITE 203" : "WHITE", "\xE2\x80\x94", std::string(), pal::InkFaint, 0u, 1.3f });
    }
    std::string peakSub;
    if (std::isfinite(l.targetPeak)) peakSub = "TARGET " + Fmt("%.0f", l.targetPeak);
    if (std::isfinite(l.neutralPeak)) peakSub += (peakSub.empty() ? "" : "  ") + std::string("NEUTRAL ") + Fmt("%.0f", l.neutralPeak);
    t.push_back({ "PEAK", NitsText(l.peak), peakSub, pal::Ink, 0u, 1.6f });
    t.push_back({ "BLACK", NitsText(l.black), std::string(), pal::Ink, 0u, 0.9f });
    return t;
}

// ---- the chart tiles ---------------------------------------------------------------------------------------------
// The charts a view opens with. A run in progress: fixed (nothing moves as data arrives). A record: the preferred
// ones that have data, then the others that do.
std::array<DlcChart, 4> DefaultTiles(const dlc::LiveCharts& c, bool live) {
    using K = DlcChart;
    const bool building = c.convergence.size() > 1;
    std::vector<K> pref;
    if (live) pref = building ? std::vector<K>{ K::Convergence, K::Bands, K::Hues, K::Stability }
                              : std::vector<K>{ K::ToneError, K::GreyTint, K::Stability, K::Bands };
    else pref = building ? std::vector<K>{ K::Bands, K::BuildDelta, K::GreyTint, K::Worst }
                         : std::vector<K>{ K::Bands, K::ToneError, K::GreyTint, K::Worst };
    std::array<K, 4> out{};
    if (live) {
        for (int i = 0; i < 4; i++) out[(size_t)i] = pref[(size_t)i];
        return out;
    }
    std::vector<K> order;
    auto add = [&](K k) { if (std::find(order.begin(), order.end(), k) == order.end()) order.push_back(k); };
    for (K k : pref) if (DlcChartHasData(k, c)) add(k);
    for (K k : { K::Bands, K::ToneError, K::GreyTint, K::Worst, K::Hues, K::Distribution, K::Cie, K::BuildDelta, K::Stability, K::Convergence })
        if (DlcChartHasData(k, c)) add(k);
    for (K k : pref) add(k);
    for (int i = 0; i < 4; i++) out[(size_t)i] = order[(size_t)i];
    return out;
}

// One tile: the chart picker, FULL / TILES, the chart. A double-click on the chart does what FULL / TILES does.
void ChartTile(ViewState& vs, int i, DlcChart k, const dlc::LiveCharts* c, bool hdr, ImVec2 ta, ImVec2 tb, const char* emptyText,
               const ChartContext& ctx) {
    constexpr float kMaxBtnW = 60.0f;
    const float tw = tb.x - ta.x;
    const bool full = vs.dlc.maximized == i;
    ImGui::PushID(i);
    ImGui::SetCursorScreenPos(ta);
    const float ddW = (tw - Px(kMaxBtnW) - Px(8)) / Dpi();
    const int pick = vs.dlc.tile[i];
    const ImVec2 ha(ta.x, ta.y), hb(ta.x + Px(ddW), ta.y + Snap(Px(kTileHeadH)));
    const bool headHover = ImGui::IsMouseHoveringRect(ha, hb) && ImGui::IsWindowHovered();
    if (BeginDropdown("##chart", DlcChartTitle(k, hdr), ddW)) {
        if (DropdownItem("AUTOMATIC", pick < 0)) vs.dlc.tile[i] = -1;
        for (int j = 0; j < (int)DlcChart::Count; j++)
            if (DropdownItem(DlcChartTitle((DlcChart)j, hdr), pick == j)) vs.dlc.tile[i] = j;
        EndDropdown();
    } else if (headHover) {
        ImGui::SetTooltip("%s", DlcChartTip(k));
    }
    ImGui::SetCursorScreenPos(ImVec2(Snap(tb.x - Px(kMaxBtnW)), ta.y));
    if (Button("##full", full ? "TILES" : "FULL", kMaxBtnW)) vs.dlc.maximized = full ? -1 : i;
    if (ImGui::IsItemHovered()) ImGui::SetTooltip(full ? "Back to four charts." : "This chart over the whole area.");
    ImGui::PopID();
    const ImVec2 pa(ta.x, ta.y + Snap(Px(kTileHeadH)) + Snap(Px(6))), pb = tb;
    if (!c || !c->present) {
        DrawFieldBox(ImGui::GetWindowDrawList(), pa, pb);
        DrawTextIn(ImGui::GetWindowDrawList(), Font::Label, kSizeLabel, pa, pb, pal::InkFaint, emptyText);
    } else {
        DrawDlcChart(k, *c, pa, pb, ctx);
        if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) && ImGui::IsMouseHoveringRect(pa, pb) && ImGui::IsWindowHovered())
            vs.dlc.maximized = full ? -1 : i;
    }
}

void ChartTiles(ViewState& vs, const dlc::LiveCharts* c, bool hdr, bool live, ImVec2 size, const char* emptyText,
                const ChartContext& ctx) {
    const ImVec2 a(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    static const dlc::LiveCharts kNone;
    const std::array<DlcChart, 4> def = DefaultTiles(c ? *c : kNone, live);
    auto chartOf = [&](int i) {
        const int pick = vs.dlc.tile[i];
        return pick >= 0 && pick < (int)DlcChart::Count ? (DlcChart)pick : def[(size_t)i];
    };
    if (vs.dlc.maximized >= 0 && vs.dlc.maximized < 4) {
        const int i = vs.dlc.maximized;
        ChartTile(vs, i, chartOf(i), c, hdr, a, ImVec2(a.x + size.x, a.y + size.y), emptyText, ctx);
    } else {
        const float gap = Snap(Px(12));
        const float tw = Snap((size.x - gap) * 0.5f), th = Snap((size.y - gap) * 0.5f);
        for (int i = 0; i < 4; i++) {
            const ImVec2 ta(a.x + (float)(i % 2) * (tw + gap), a.y + (float)(i / 2) * (th + gap));
            ChartTile(vs, i, chartOf(i), c, hdr, ta, ImVec2(ta.x + tw, ta.y + th), emptyText, ctx);
        }
    }
    ImGui::SetCursorScreenPos(ImVec2(a.x, a.y + size.y));
    ImGui::Dummy(ImVec2(size.x, 0));
}

// ---- right column: the feed, and a seam while one waits ----------------------------------------------------------
void SeamRow(const char* label, const std::string& value, ImU32 col = pal::Ink, Font f = Font::Mono) {
    RowCtx row = BeginRow("", kRowH);
    TextAt(Font::Label, kSizeLabel, pal::InkDim, row.x0, row.y0 + row.h * 0.5f, label);
    row.x = row.x0 + Px(96);
    RowText(row, value, col, f, f == Font::Mono ? kSizeMono : kSizeLabel, Wide(value.c_str()));
    EndRow(row);
}

void Wrapped(const std::string& text, ImU32 col, int maxLines) {
    ImGui::PushFont(GetFont(Font::Body), 13.0f);
    ImGui::PushStyleColor(ImGuiCol_Text, Col(col));
    const float w = ImGui::GetContentRegionAvail().x;
    const ImVec2 sz = ImGui::CalcTextSize(text.c_str(), nullptr, false, w);
    const float lineH = ImGui::GetTextLineHeight();
    const float maxH = lineH * (float)maxLines;
    const ImVec2 p = ImGui::GetCursorScreenPos();
    ImGui::PushClipRect(p, ImVec2(p.x + w, p.y + std::min(sz.y, maxH)), true);
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + w);
    ImGui::TextUnformatted(text.c_str());
    ImGui::PopTextWrapPos();
    ImGui::PopClipRect();
    ImGui::PopStyleColor();
    ImGui::PopFont();
    if (sz.y > maxH) {
        ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + maxH));
        if (ImGui::IsMouseHoveringRect(p, ImVec2(p.x + w, p.y + maxH)))
            ImGui::SetTooltip("%s", text.c_str());
    }
    ImGui::Dummy(ImVec2(0, Px(4)));
}

void SeamCard(const dlc::SeamPacket& s, const dlc::ActivePointer& a) {
    if (!s.question.empty()) Wrapped(s.question, pal::Ink, 5);
    if (!s.options.empty()) {
        std::string opts;
        for (const std::string& o : s.options) opts += (opts.empty() ? "" : "  \xC2\xB7  ") + o;
        SeamRow("OPTIONS", opts, pal::InkDim);
    }
    if (!s.recommendation.empty()) SeamRow("RECOMMENDED", s.recommendation, pal::Ink);
    if (!s.checklist.empty()) {   // DLC's steps are sentences: wrapped, not cut
        ImGui::Dummy(ImVec2(0, Px(4)));
        Text(Font::Label, kSizeLabel, pal::InkDim, "CHECKLIST");
        ImGui::Dummy(ImVec2(0, Px(2)));
        for (size_t i = 0; i < s.checklist.size() && i < 8; i++) Wrapped("\xE2\x80\xA2 " + s.checklist[i], pal::InkDim, 3);
    }
    const std::string driver = DriverTag(a);
    SeamRow("DECIDES", driver.empty() || driver == "CLAUDE" ? std::string("CLAUDE") : driver, pal::Ink, Font::Label);
}

void FeedRows(const dlc::LiveState& st) {
    if (st.feed.empty()) {
        Text(Font::Label, kSizeLabel, pal::InkFaint, "NOTHING YET");
        return;
    }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float w = ImGui::GetContentRegionAvail().x, h = Snap(Px(kFeedRowH));
    for (size_t k = st.feed.size(); k-- > 0;) {   // newest first
        const dlc::FeedRow& f = st.feed[k];
        const ImVec2 p(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
        const bool warn = f.level == "WARN" || f.level == "ERROR";
        const bool claude = f.event == "supervisor_note";
        const std::string tm = Hhmm(f.time);
        const float tmW = MeasureInk(Font::Mono, 11.0f, "00:00").advance;
        TextAt(Font::Mono, 11.0f, pal::InkFaint, p.x + w - tmW, p.y + h * 0.30f, tm.c_str());
        const std::string title = Ellipsize(Font::Label, 11.0f, Upper(f.title), w - tmW - Px(10));
        TextAt(Font::Label, 11.0f, warn ? pal::Alarm : (claude ? pal::Ink : pal::InkDim), p.x, p.y + h * 0.30f, title.c_str());
        if (!f.text.empty()) {
            const Font tf = claude ? Font::Body : Font::Mono;
            const float ts = claude ? 12.5f : 11.5f;
            TextAt(tf, ts, claude ? pal::Ink : pal::InkDim, p.x, p.y + h * 0.72f, Ellipsize(tf, ts, f.text, w).c_str());
        }
        ImGui::Dummy(ImVec2(w, h));
        if (ImGui::IsItemHovered() && (!f.text.empty()))
            ImGui::SetTooltip("%s\n%s", f.title.c_str(), f.text.c_str());
        dl->AddRectFilled(ImVec2(p.x, p.y + h - 1), ImVec2(p.x + w, p.y + h), Col(pal::Rule));
    }
}

// "20261012_140312_..." -> "2026-10-12 14:03" (the run id as it is otherwise).
std::string RunWhen(const std::string& id) {
    if (id.size() >= 13 && id[8] == '_')
        return id.substr(0, 4) + "-" + id.substr(4, 2) + "-" + id.substr(6, 2) + " " + id.substr(9, 2) + ":" + id.substr(11, 2);
    return id;
}

// A layer of the stack: its light, what it is and where it came from.
void LayerRow(const char* label, Light light, const std::string& value, ImU32 col, const std::string& tip) {
    RowCtx row = BeginRow("", kRowH);
    const float cy = row.y0 + row.h * 0.5f;
    TextAt(Font::Label, kSizeLabel, pal::InkDim, row.x0, cy, label);
    const float d = Snap(Px(7));
    DrawLight(ImGui::GetWindowDrawList(), ImVec2(Snap(row.x0 + Px(96) + d * 0.5f), cy), light, d);
    row.x = row.x0 + Px(96) + d + Px(8);
    RowText(row, value, col, Font::Label, kSizeLabel);
    if (!tip.empty()) RowTip(row, tip.c_str());
    EndRow(row);
}

// The layers this display runs in the shown mode, in pixel order; DLC's own with the run that made them.
void StackLayers(const Snapshot& snap, const DisplayRow& r, bool showHdr) {
    const dlc::Data& d = *snap.dlc;
    const dlc::ModeRecord& rec = r.dlcRecord[showHdr ? 1 : 0];
    const dlc::StackRecord* st = rec.stack >= 0 && rec.stack < (int)d.stacks.size() ? &d.stacks[(size_t)rec.stack] : nullptr;
    const Mode shown = showHdr ? Mode::HDR : (r.mode == Mode::AcmSdr ? Mode::AcmSdr : Mode::SDR);
    PathRuntime rt;
    rt.suspended = snap.suspended;
    rt.running = snap.running;
    rt.hookMode = snap.hookMode;
    rt.hookActive = snap.hookActive;
    const std::vector<Stage> path = BuildPixelPath(r.settings, shown, r.mode, rt);
    auto onOff = [](Light l) -> std::pair<std::string, ImU32> {
        switch (l) {
        case Light::Configured:
        case Light::Verified: return { "ON", pal::Ink };
        case Light::Fault:    return { "NOT REACHING THE SCREEN", pal::Alarm };
        case Light::Off:      return { "OFF", pal::InkFaint };
        default:              return { "\xE2\x80\x94", pal::InkFaint };
        }
    };
    auto dlcLayer = [&](dlc::LayerState s, const std::string& runId, Light l) -> std::pair<std::string, ImU32> {
        if (s == dlc::LayerState::Off) return onOff(l);
        std::string v = runId.empty() ? std::string() : RunWhen(runId) + "  \xC2\xB7  ";
        switch (s) {
        case dlc::LayerState::Matches:    return { v + "AS APPLIED", l == Light::Fault ? pal::Alarm : pal::Ink };
        case dlc::LayerState::Differs:    return { v + "CHANGED", pal::Alarm };
        case dlc::LayerState::Unrecorded: return { "NOT FROM DLC", pal::InkDim };
        default:                          return onOff(l);
        }
    };
    Light mhc = Light::None;
    for (const Stage& s : path) if (s.id == StageId::Primaries || s.id == StageId::Grayscale) mhc = CombineLights(mhc, s.light);
    bool mhcDone = false;
    for (const Stage& s : path) {
        switch (s.id) {
        case StageId::ToneMap: {
            const auto v = onOff(s.light);
            LayerRow("TONE MAP", s.light, v.first, v.second, "Applied over the calibration; a verification measures it only when it ran then.");
            break;
        }
        case StageId::Lut3D: {
            const auto v = dlcLayer(rec.cube, st ? st->cubeRunId : std::string(), s.light);
            LayerRow("3D LUT", s.light, v.first, v.second, st && !st->cubeRunId.empty() ? "DLC run " + st->cubeRunId : std::string());
            break;
        }
        case StageId::Fald: {
            const auto v = onOff(s.light);
            LayerRow("FALD", s.light, v.first, v.second, std::string());
            break;
        }
        case StageId::ShaderTweak: {
            const auto v = onOff(s.light);
            LayerRow("TWEAK", s.light, v.first, v.second, "The grayscale tweak over the 3D LUT.");
            break;
        }
        case StageId::Primaries:
        case StageId::Grayscale: {
            if (mhcDone) break;
            mhcDone = true;
            const auto v = dlcLayer(rec.mhc, st ? st->runId : std::string(), mhc);
            LayerRow("MHC", mhc, v.first, v.second, st && !st->runId.empty() ? "DLC run " + st->runId : std::string());
            break;
        }
        default: break;
        }
    }
}

// ---- right column, record view: what DLC recorded for this display + the way to start a run ---------------------
// `stack`: the CURRENT STACK view — the layers in pixel order and the verification that measured them.
void RecordPanelRows(const Snapshot& snap, const DisplayRow& r, bool showHdr, ViewState& vs, bool stack = false) {
    const dlc::Data& d = *snap.dlc;
    if (d.folder.empty()) {
        SeamRow("DLC", "NO FOLDER", pal::InkFaint, Font::Label);
        RowCtx row = BeginRow("", kRowH);
        row.x = row.x0 + Px(96);
        RowPlace(row);
        if (Button("##dlc_settings", "SETTINGS", kButtonW)) vs.page = Page::Settings;
        EndRow(row);
        return;
    }
    if (r.dlcMatch.name.empty()) {
        SeamRow("DLC", r.dlcMatch.ambiguous ? "AMBIGUOUS" : "NOT MATCHED", pal::InkFaint, Font::Label);
        return;
    }
    const dlc::ModeRecord& rec = r.dlcRecord[showHdr ? 1 : 0];
    SeamRow("DLC", r.dlcMatch.name, pal::Ink, Font::Label);
    if (rec.reminderDue) SeamRow("DUE", "RECALIBRATE", pal::Alarm, Font::Label);
    else if (g_recalReminderDays > 0 && rec.verifiedDaysAgo >= 0)
        SeamRow("DUE", "IN " + std::to_string(std::max(0, g_recalReminderDays - rec.verifiedDaysAgo)) + " D", pal::InkDim, Font::Label);
    auto layer = [](dlc::LayerState s) {
        switch (s) {
        case dlc::LayerState::Matches: return "AS APPLIED";
        case dlc::LayerState::Differs: return "CHANGED";
        case dlc::LayerState::Unrecorded: return "NOT FROM DLC";
        default: return "OFF";
        }
    };
    if (stack) {
        if (rec.current >= 0) {
            const dlc::Report& cur = d.reports[(size_t)rec.current];
            std::string when = cur.date.substr(0, 10);
            const std::string id = dlc::RunIdFromPath(cur.runDir);
            if (id.size() >= 13 && id[8] == '_') when = RunWhen(id);
            SeamRow("VERIFIED", when, pal::Ink, Font::Label);
        } else {
            SeamRow("VERIFIED", "NOT SINCE APPLIED", pal::InkFaint, Font::Label);
        }
        ImGui::Dummy(ImVec2(0, Px(4)));
        Hairline(pal::Rule);
        ImGui::Dummy(ImVec2(0, Px(4)));
        StackLayers(snap, r, showHdr);
        ImGui::Dummy(ImVec2(0, Px(4)));
        Hairline(pal::Rule);
        ImGui::Dummy(ImVec2(0, Px(4)));
    } else {
        SeamRow("MHC", layer(rec.mhc), rec.mhc == dlc::LayerState::Differs ? pal::Alarm : pal::Ink, Font::Label);
        SeamRow("3D LUT", layer(rec.cube), rec.cube == dlc::LayerState::Differs ? pal::Alarm : pal::Ink, Font::Label);
    }
    if (!rec.correctionMade.empty())
        SeamRow("METER", DateAge(rec.correctionMade, rec.correctionDaysAgo), rec.correctionStale ? pal::Alarm : pal::InkDim);
    // Runs are started from Claude Code: the prompt, ready to paste.
    RowCtx row = BeginRow("", kRowH);
    TextAt(Font::Label, kSizeLabel, pal::InkDim, row.x0, row.y0 + row.h * 0.5f, "START");
    row.x = row.x0 + Px(96);
    RowPlace(row);
    if (Button("##dlc_prompt", "COPY PROMPT", 116)) {
        const std::string prompt = "Calibrate " + r.dlcMatch.name + " (" + (showHdr ? "HDR" : "SDR") + ") with DLC.";
        ImGui::SetClipboardText(prompt.c_str());
        notify::Post(notify::Sev::Info, -1, L"dlc", L"Prompt copied", Wide(prompt.c_str()));
    }
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("For Claude Code: it drives DLC and decides its seams.");
    EndRow(row);
}

// The reports for one DLC display and mode, newest first (indices into Data::reports).
std::vector<int> ReportsFor(const dlc::Data& d, const std::string& name, bool hdr) {
    std::vector<int> out;
    for (int i = 0; i < (int)d.reports.size(); i++) {
        const dlc::Report& r = d.reports[i];
        if (_stricmp(r.display.c_str(), name.c_str()) != 0) continue;
        if (dlc::NormMode(r.mode) != (hdr ? "HDR" : "SDR")) continue;
        out.push_back(i);
    }
    return out;
}

// "2026-10-03 21:28 · VERIFY-ONLY · SDR CONTENT · 333": the run's start time tells same-day runs apart, and the
// content mode says when a run measured the other mode's content (SDR content on the HDR desktop).
std::string ReportLabel(const dlc::Report& r) {
    std::string s = r.date.substr(0, 10);
    const std::string id = dlc::RunIdFromPath(r.runDir);   // YYYYMMDD_HHMMSS_...
    if (id.size() >= 13 && id[8] == '_') s += " " + id.substr(9, 2) + ":" + id.substr(11, 2);
    s += "  \xC2\xB7  " + Upper(r.flow);
    if (!r.contentMode.empty() && dlc::NormMode(r.contentMode) != dlc::NormMode(r.mode))
        s += "  \xC2\xB7  " + Upper(r.contentMode) + " CONTENT";
    if (r.verification.patchCount > 0) s += "  \xC2\xB7  " + std::to_string(r.verification.patchCount);
    return s;
}

}  // namespace

Light DlcRunLight(const Snapshot& snap) {
    const dlc::Live& L = RunOf(snap);
    if (!InProgress(L.phase) && L.phase != dlc::RunPhase::Failed) return Light::None;
    if (L.phase == dlc::RunPhase::Failed) {
        // A failure is news until the next run, but not forever: only while the state file is recent.
        if (!std::isfinite(L.stateAgeS) || L.stateAgeS > 3600.0) return Light::None;
    }
    return LookOf(L).light;
}

bool DlcRunBadge(const Snapshot& snap, const DisplayRow& r, std::string& text, ImU32& col) {
    const dlc::Live& L = RunOf(snap);
    if (!r.dlcRun || !InProgress(L.phase)) return false;
    const Look look = LookOf(L);
    text = look.word == "LIVE" ? "MEASURING" : look.word;
    if (L.state && L.state->patchesTotal > 0 && L.phase == dlc::RunPhase::Running) {
        const int pct = (int)std::lround(100.0 * L.state->patchesDone / std::max(1, L.state->patchesTotal));
        text += "  " + std::to_string(pct) + "%";
    }
    col = look.light == Light::Fault ? pal::Alarm : (look.word == "LIVE" ? pal::Good : pal::InkDim);
    return true;
}

void DrawDlcPage(const Snapshot& snap, ViewState& vs) {
    const DisplayRow* sel = SelectedDisplay(snap, vs);
    const ImVec2 body(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float bodyH = PageFooterTop() - body.y;
    DrawDisplayList(snap, vs, sel, body, bodyH);

    ImGui::SetCursorScreenPos(ImVec2(body.x + Snap(Px(kSidebarW)), body.y));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(kGutter), Px(18)));
    ImGui::BeginChild("##dlc_main", ImVec2(0, bodyH), ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PopStyleVar();
    if (!sel) {
        BeginPanel("##dlc_none", "NO DISPLAYS", nullptr, ImVec2(0, 0));
        Text(Font::Label, kSizeLabel, pal::InkFaint, "NOTHING CONNECTED OR REMEMBERED");
        EndPanel();
        ImGui::EndChild();
        return;
    }
    const dlc::Live& L = RunOf(snap);
    const dlc::Data& d = *snap.dlc;
    const DisplayRow* runRow = RunRow(snap);

    // Title row; a note when the interface sits on the display being measured (it would be in the meter's view).
    const TitleRow title = DrawDisplayTitle(*sel, vs);
    const bool showHdr = title.showHdr;
    if (runRow && InProgress(L.phase) && snap.live.size() > 1 && Window() &&
        MonitorFromWindow(Window(), MONITOR_DEFAULTTONEAREST) == runRow->hmon) {
        const float nw = DrawText(ImGui::GetWindowDrawList(), Font::Label, kSizeLabel, title.noteX, title.cy, pal::Alarm, "ON MEASURED DISPLAY");
        HMONITOR other = nullptr;
        for (const DisplayRow& r : snap.live) if (r.hmon && r.hmon != runRow->hmon && (!other || r.primary)) other = r.hmon;
        const ImVec2 keep = ImGui::GetCursorScreenPos();
        ImGui::SetCursorScreenPos(ImVec2(Snap(title.noteX + nw + Px(12)), Snap(title.cy - Px(kControlH) * 0.5f)));
        const HMONITOR measured = runRow->hmon;
        if (other && Button("##dlc_move", "MOVE", 72)) Post([measured, other]() { MoveWindowOff(measured, other); });
        if (other && ImGui::IsItemHovered()) ImGui::SetTooltip("Move this window to another display.");
        ImGui::SetCursorScreenPos(keep);
    }

    RunStrip(snap, vs, sel);
    ImGui::Dummy(ImVec2(0, Px(12)));

    // Which view: the live run (this display's run, picked or automatic while in progress), the current stack (what
    // the display runs now and the newest verification of it), or one record.
    const bool runHere = sel->dlcRun && L.state && L.state->present && ((L.state->mode == "HDR") == showHdr);
    std::vector<int> reports = sel->dlcMatch.name.empty() ? std::vector<int>() : ReportsFor(d, sel->dlcMatch.name, showHdr);
    const dlc::ModeRecord& rec = sel->dlcRecord[showHdr ? 1 : 0];
    const bool hasStack = !d.folder.empty() && !sel->dlcMatch.name.empty() && (rec.stack >= 0 || rec.current >= 0);
    bool liveView = false, stackView = false;
    int shown = -1;
    if (vs.dlc.pick == kDlcPickLive) liveView = runHere;
    else if (vs.dlc.pick == kDlcPickStack) stackView = hasStack;
    else if (!vs.dlc.pick.empty()) {
        for (int i : reports) if (dlc::SamePath(d.reports[i].path, vs.dlc.pick)) shown = i;
    }
    if (!liveView && !stackView && shown < 0) {
        if (vs.dlc.pick.empty() && runHere && InProgress(L.phase)) liveView = true;
        else if (hasStack) stackView = true;
        else {
            shown = reports.empty() ? -1 : reports.front();
            if (shown < 0 && runHere) liveView = true;
        }
    }
    if (stackView) shown = rec.current;
    const Look look = LookOf(L);

    // Second row: the stepper (live) or the record picker.
    if (liveView) {
        // The stepper; at the line's end the way to the records (when there are any).
        const ImVec2 p(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
        const float w = ImGui::GetContentRegionAvail().x, h = Snap(Px(kStepsH));
        const float btnW = reports.empty() ? 0.0f : Snap(Px(kButtonW)) + Snap(Px(16));
        ImGui::BeginChild("##dlc_steps", ImVec2(w - btnW, h), ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse | ImGuiWindowFlags_NoBackground);
        Stepper(*L.state, look);
        ImGui::EndChild();
        if (!reports.empty()) {
            ImGui::SetCursorScreenPos(ImVec2(Snap(p.x + w - Px(kButtonW)), Snap(p.y + (h - Px(kControlH)) * 0.5f)));
            if (Button("##dlc_runs", "RUNS", kButtonW)) vs.dlc.pick = d.reports[reports.front()].path;
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("This display's past runs.");
        }
        ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h));
        ImGui::Dummy(ImVec2(w, 0));
    } else {
        const ImVec2 p(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
        const float w = ImGui::GetContentRegionAvail().x, h = Snap(Px(kStepsH)), cy = p.y + h * 0.5f;
        TextAt(Font::Label, kSizeLabel, pal::InkDim, p.x, cy, "RUN");
        ImGui::SetCursorScreenPos(ImVec2(Snap(p.x + Px(48)), Snap(cy - Px(kControlH) * 0.5f)));
        const std::string preview = stackView ? std::string("CURRENT STACK") : shown >= 0 ? ReportLabel(d.reports[shown]) : std::string("NO RUNS");
        ImGui::BeginDisabled(reports.empty() && !runHere && !hasStack);
        if (BeginDropdown("##dlc_run", preview.c_str(), kRunPickerW)) {
            if (runHere && DropdownItem(("LIVE  \xC2\xB7  " + Upper(L.state->flow)).c_str(), false)) vs.dlc.pick = kDlcPickLive;
            if (hasStack && DropdownItem("CURRENT STACK", stackView)) vs.dlc.pick = kDlcPickStack;
            for (int i : reports) {
                ImGui::PushID(i);   // two runs can share a label (same day, same flow): ids by report
                if (DropdownItem(ReportLabel(d.reports[i]).c_str(), !stackView && i == shown)) vs.dlc.pick = d.reports[i].path;
                ImGui::PopID();
            }
            EndDropdown();
        }
        ImGui::EndDisabled();
        if (ImGui::IsItemHovered() && stackView)
            ImGui::SetTooltip("What the display runs now: its layers and the newest verification of them.");
        float x = Snap(p.x + Px(48) + Px(kRunPickerW) + Px(12));
        if (stackView && shown >= 0) {   // the verification the stack's charts come from
            const std::string from = "VERIFIED  " + ReportLabel(d.reports[shown]);
            TextAt(Font::Label, kSizeLabel, pal::InkDim, x, cy,
                   Ellipsize(Font::Label, kSizeLabel, from, p.x + w - Px(kButtonW) - Px(16) - x).c_str());
        } else if (stackView) {
            TextAt(Font::Label, kSizeLabel, pal::InkFaint, x, cy, "NOT VERIFIED SINCE APPLIED");
        }
        if (shown >= 0) {
            const char* tag = !stackView && shown == rec.current ? "ON SCREEN" : nullptr;
            if (tag) {
                ImDrawList* dl = ImGui::GetWindowDrawList();
                const InkBox ib = MeasureInk(Font::Label, 11.0f, tag);
                const ImVec2 ta(x, Snap(cy - Px(9))), tb(Snap(x + ib.advance + Px(12)), Snap(cy + Px(9)));
                dl->AddRect(ta, tb, Col(pal::Good));
                DrawTextIn(dl, Font::Label, 11.0f, ta, tb, pal::Good, tag);
                if (ImGui::IsMouseHoveringRect(ta, tb) && ImGui::IsWindowHovered())
                    ImGui::SetTooltip("This run verified what the display runs now.");
            }
            const dlc::Report& rep = d.reports[shown];
            ImGui::SetCursorScreenPos(ImVec2(Snap(p.x + w - Px(kButtonW)), Snap(cy - Px(kControlH) * 0.5f)));
            if (Button("##dlc_folder", "FOLDER", kButtonW)) {
                const std::wstring html = rep.folder + L"\\report.html";
                const std::wstring target = FileExists(html) ? html : rep.path;
                Post([target]() { OpenFolderSelecting(target); });
            }
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("The run's results folder: report, cube, measurements.");
        }
        ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h));
        ImGui::Dummy(ImVec2(w, 0));
    }
    ImGui::Dummy(ImVec2(0, Px(10)));

    // The charts (they also feed the readouts).
    const dlc::Report* rep = shown >= 0 ? &d.reports[shown] : nullptr;
    std::shared_ptr<const dlc::LiveCharts> charts;
    const char* emptyText = "NO CHARTS";
    if (liveView) {
        charts = L.charts;
        emptyText = "WAITING FOR READS";
    } else if (rep && !rep->runDir.empty()) {
        // The run's final charts when it has them, else charts from its patch metrics.
        charts = dlc::RunCharts(rep->runDir);
        if (!charts) emptyText = "LOADING";
        else if (!charts->present) {
            static std::shared_ptr<const std::vector<dlc::Patch>> s_from;
            static std::shared_ptr<const dlc::LiveCharts> s_built;
            const auto patches = dlc::Patches(rep->runDir);
            if (!patches) { charts = nullptr; emptyText = "LOADING"; }
            else {
                if (patches != s_from) {
                    s_from = patches;
                    s_built = dlc::ChartsFromPatches(*patches, dlc::NormMode(rep->mode) == "HDR");
                }
                charts = s_built;
            }
        }
    } else if (!rep) {
        emptyText = stackView ? "NOT VERIFIED SINCE APPLIED"
                    : d.folder.empty() ? "NO DLC FOLDER" : (sel->dlcMatch.name.empty() ? "NOT A DLC DISPLAY" : "NO RUNS");
    }
    const bool chartsHdr = liveView ? L.state->hdr : showHdr;

    // Readouts: the run's live numbers; a record's practical view (its report's numbers when it has none).
    const dlc::LiveCharts* lc = charts && charts->present ? charts.get() : nullptr;
    if (liveView) ReadoutRow(LiveTiles(*L.state, lc));
    else if (lc && lc->practical.present) ReadoutRow(PracticalTiles(lc->practical, rep, chartsHdr));
    else ReadoutRow(RecordTiles(rep));
    ImGui::Dummy(ImVec2(0, Px(14)));

    // Charts left, the feed / record panel right (under the charts in a narrow window).
    const ImVec2 top(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float total = ImGui::GetContentRegionAvail().x, colH = std::max(ImGui::GetContentRegionAvail().y, Px(360));
    const float sideW = Snap(Px(kMeasuredW)), gutter = Snap(Px(kGutter));
    const bool stacked = total - sideW - gutter < Px(440);
    const float chartsW = stacked ? total : total - sideW - gutter;
    ChartContext ctx;
    if (liveView) {
        ctx.settled = L.state->settled;
        ctx.slopePct10 = L.state->driftSlopePct10;
        ctx.channel = L.state->driftChannel.c_str();
    }
    ChartTiles(vs, charts.get(), chartsHdr, liveView, ImVec2(chartsW, stacked ? Px(440) : colH), emptyText, ctx);
    if (stacked) ImGui::Dummy(ImVec2(0, Px(kPanelGap)));
    else ImGui::SetCursorScreenPos(ImVec2(top.x + chartsW + gutter, top.y));
    const ImVec2 sideSize(stacked ? total : sideW, stacked ? 0.0f : colH);
    if (liveView) {
        const bool seam = L.state->seam.Pending();
        const Light light = seam ? Light::Pending : Light::None;
        const std::string seamTitle = Upper(L.state->seam.key);
        BeginPanel("##dlc_feed", seam ? seamTitle.c_str() : "FEED", seam ? &light : nullptr, sideSize);
        if (seam) {
            SeamCard(L.state->seam, L.active);
            ImGui::Dummy(ImVec2(0, Px(6)));
            Hairline(pal::Rule);
            ImGui::Dummy(ImVec2(0, Px(6)));
        }
        FeedRows(*L.state);
        EndPanel();
    } else {
        const Light light = rec.reminderDue ? Light::Fault : (rec.current >= 0 ? Light::Configured : Light::None);
        BeginPanel(stackView ? "##dlc_stack" : "##dlc_record", stackView ? "CURRENT STACK" : "CALIBRATION", &light, sideSize);
        RecordPanelRows(snap, *sel, showHdr, vs, stackView);
        EndPanel();
    }
    ImGui::EndChild();
}

}  // namespace ui
