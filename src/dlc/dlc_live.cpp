#include "dlc/dlc_live.h"

#include "ipc_json.h"

#include <windows.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <map>
#include <cstdio>
#include <mutex>
#include <thread>

namespace dlc {

using ipc_json::JsonOptions;
using ipc_json::JsonParser;
using ipc_json::JsonValue;

namespace {

constexpr uint64_t kStateBytes = 8ull << 20;    // state.json: tens of KB in practice
constexpr uint64_t kChartsBytes = 32ull << 20;  // charts.json: ~100 KB for a dense run
constexpr uint64_t kSmallBytes = 64ull << 10;   // active.json / control.json
constexpr double kNoDataAfterS = 15.0;          // the publisher rewrites state at least every 2 s

bool ParseJson(const std::string& text, JsonValue& out) {
    try {
        JsonParser p(text, JsonOptions::Files());
        out = p.parse();
        return out.type == JsonValue::Obj;
    } catch (...) {
        return false;
    }
}

const JsonValue* Get(const JsonValue* v, const char* key, JsonValue::Type type) {
    const JsonValue* c = v ? v->find(key) : nullptr;
    return c && c->type == type ? c : nullptr;
}
const JsonValue* Obj(const JsonValue* v, const char* key) { return Get(v, key, JsonValue::Obj); }
const JsonValue* Arr(const JsonValue* v, const char* key) { return Get(v, key, JsonValue::Arr); }

std::string Str(const JsonValue* v, const char* key) {
    const JsonValue* c = Get(v, key, JsonValue::Str);
    return c ? c->str : std::string();
}

double Num(const JsonValue* v, const char* key) {
    const JsonValue* c = Get(v, key, JsonValue::Num);
    return c ? c->num : kNaN;
}

double NumAt(const JsonValue& arr, size_t i) {
    return arr.type == JsonValue::Arr && i < arr.arr.size() && arr.arr[i].type == JsonValue::Num ? arr.arr[i].num : kNaN;
}

int Int(const JsonValue* v, const char* key, int def) {
    const double d = Num(v, key);
    return std::isfinite(d) ? (int)std::llround(d) : def;
}

bool Bool(const JsonValue* v, const char* key) {
    const JsonValue* c = Get(v, key, JsonValue::Bool);
    return c && c->b;
}

float F(double d) { return (float)d; }

// "#rrggbb" -> 0xRRGGBB; 0 when unreadable.
uint32_t Hex(const std::string& s) {
    if (s.size() != 7 || s[0] != '#') return 0;
    unsigned v = 0;
    for (size_t i = 1; i < 7; i++) {
        const char c = s[i];
        const unsigned d = (c >= '0' && c <= '9') ? (unsigned)(c - '0')
                         : (c >= 'a' && c <= 'f') ? (unsigned)(c - 'a' + 10)
                         : (c >= 'A' && c <= 'F') ? (unsigned)(c - 'A' + 10) : 16u;
        if (d > 15) return 0;
        v = v * 16 + d;
    }
    return v ? v : 0x010101;   // pure black stays distinguishable from "unknown"
}

uint32_t SignalHex(const double s[3]) {
    if (!std::isfinite(s[0]) || !std::isfinite(s[1]) || !std::isfinite(s[2])) return 0;
    auto c = [](double v) { return (uint32_t)std::clamp((int)std::lround(v * 255.0), 0, 255); };
    const uint32_t v = (c(s[0]) << 16) | (c(s[1]) << 8) | c(s[2]);
    return v ? v : 0x010101;
}

void Xy(const JsonValue* arr, float out[2]) {
    if (!arr || arr->type != JsonValue::Arr || arr->arr.size() < 2) return;
    const double x = NumAt(*arr, 0), y = NumAt(*arr, 1);
    if (std::isfinite(x) && std::isfinite(y)) { out[0] = F(x); out[1] = F(y); }
}

bool Primaries(const JsonValue* obj, float out[3][2]) {
    if (!obj || obj->type != JsonValue::Obj) return false;
    static const char* kKeys[3] = { "r", "g", "b" };
    for (int i = 0; i < 3; i++) {
        const JsonValue* a = Arr(obj, kKeys[i]);
        if (!a || a->arr.size() < 2 || !std::isfinite(NumAt(*a, 0)) || !std::isfinite(NumAt(*a, 1))) return false;
        out[i][0] = F(NumAt(*a, 0));
        out[i][1] = F(NumAt(*a, 1));
    }
    return true;
}

std::vector<std::string> Strings(const JsonValue* arr) {
    std::vector<std::string> out;
    if (!arr || arr->type != JsonValue::Arr) return out;
    for (const JsonValue& v : arr->arr) {
        if (v.type == JsonValue::Str) out.push_back(v.str);
        else if (v.type == JsonValue::Obj) {   // checklist items may be {"text": ...} / {"item": ...}
            std::string t = Str(&v, "text");
            if (t.empty()) t = Str(&v, "item");
            if (!t.empty()) out.push_back(t);
        }
    }
    return out;
}

Zone ZoneOf(const std::string& z) {
    if (z == "limits") return Zone::Limits;
    if (z == "clamped") return Zone::Clamped;
    if (z == "peak") return Zone::Peak;
    if (z == "anchor") return Zone::Anchor;
    return Zone::Core;
}

void ParsePractical(const JsonValue& pr, Practical& out) {
    out.contentWeighted = Str(&pr, "weighting") == "content";
    out.score = F(Num(&pr, "score"));
    out.coverage = F(Num(&pr, "coverage"));
    out.offsetL = F(Num(&pr, "offset_L"));
    out.n = Int(&pr, "n", 0);
    if (const JsonValue* bands = Arr(&pr, "bands")) {
        for (const JsonValue& b : bands->arr) {
            if (b.type != JsonValue::Obj) continue;
            const double hi = Num(&b, "hi");
            out.bands.push_back({ Str(&b, "label"), F(Num(&b, "lo")), std::isfinite(hi) ? F(hi) : INFINITY,
                                  F(Num(&b, "share")), F(Num(&b, "mean")), F(Num(&b, "p95")), F(Num(&b, "L")),
                                  F(Num(&b, "C")), F(Num(&b, "H")), Int(&b, "n", 0), F(Num(&b, "Lr")) });
        }
    }
    if (const JsonValue* hues = Arr(&pr, "hues")) {
        for (const JsonValue& h : hues->arr) {
            if (h.type != JsonValue::Obj) continue;
            out.hues.push_back({ Int(&h, "sector", 0), Str(&h, "name"), Int(&h, "n", 0), F(Num(&h, "w")),
                                 F(Num(&h, "de")), F(Num(&h, "C")), F(Num(&h, "H")) });
        }
    }
    auto worst = [](const JsonValue* arr, std::vector<WorstRow>& rows) {
        if (!arr) return;
        for (const JsonValue& r : arr->arr) {
            if (r.type != JsonValue::Obj) continue;
            const std::string comp = Str(&r, "comp");
            rows.push_back({ Str(&r, "desc"), Str(&r, "label"), F(Num(&r, "de")), F(Num(&r, "impact")), F(Num(&r, "w")),
                             F(Num(&r, "dY")), comp.empty() ? ' ' : comp[0], ZoneOf(Str(&r, "zone")),
                             Hex(Str(&r, "sc")), Hex(Str(&r, "mc")) });
        }
    };
    worst(Arr(&pr, "worst"), out.worst);
    worst(Arr(&pr, "worst_apart"), out.apart);
    if (const JsonValue* lim = Obj(&pr, "limits")) {
        PanelLimits& l = out.limits;
        l.peak = F(Num(lim, "peak"));
        l.targetPeak = F(Num(lim, "target_peak"));
        l.neutralPeak = F(Num(lim, "neutral_peak"));
        l.black = F(Num(lim, "black"));
        if (const JsonValue* w = Obj(lim, "white")) {
            l.hasWhite = true;
            l.whiteY = F(Num(w, "Y"));
            l.whiteTargetY = F(Num(w, "tY"));
            l.whiteDe = F(Num(w, "de"));
            l.whiteCct = F(Num(w, "cct"));
            l.whiteDuv = F(Num(w, "duv"));
            l.whiteTw = F(Num(w, "tw"));
            l.whiteTm = F(Num(w, "tm"));
            l.whiteDY = F(Num(w, "dY"));
        }
    }
    if (const JsonValue* c = Obj(&pr, "content")) {
        out.contentClass = Str(c, "class");
        out.contentScore = F(Num(c, "score"));
    }
    out.present = !out.bands.empty();
}

}  // namespace

// ---- parsers ---------------------------------------------------------------------------------------

bool ParseActivePointer(const std::string& json, ActivePointer& out) {
    out = {};
    JsonValue root;
    if (!ParseJson(json, root)) return false;
    out.run = Utf8ToWide(Str(&root, "run"));
    if (out.run.empty()) {
        const std::wstring events = Utf8ToWide(Str(&root, "events"));
        const size_t cut = events.find_last_of(L"\\/");
        if (cut != std::wstring::npos) out.run = events.substr(0, cut);
    }
    if (out.run.empty()) return false;
    out.flow = Str(&root, "flow");
    out.updated = Str(&root, "updated");
    out.state = Str(&root, "state");
    out.display = Str(&root, "display");
    out.hardwareId = Str(&root, "hardware_id");
    out.mode = NormMode(Str(&root, "mode"));
    out.driver = Str(&root, "driver");
    out.started = Str(&root, "started");
    out.monitor = Int(&root, "monitor", -1);
    const double pid = Num(&root, "pid");
    out.pid = std::isfinite(pid) && pid > 0 && pid < 4294967296.0 ? (uint32_t)pid : 0;
    out.present = true;
    return true;
}

bool ParseLiveState(const std::string& json, LiveState& out) {
    out = {};
    JsonValue root;
    if (!ParseJson(json, root)) return false;
    out.schema = Int(&root, "live_schema", 0);
    if (out.schema <= 0) return false;
    const double seq = Num(&root, "seq");
    out.seq = std::isfinite(seq) && seq > 0 ? (uint64_t)seq : 0;
    const double pid = Num(&root, "pid");
    out.pid = std::isfinite(pid) && pid > 0 && pid < 4294967296.0 ? (uint32_t)pid : 0;
    out.runDir = Utf8ToWide(Str(&root, "run_dir"));
    out.runId = Str(&root, "run_id");
    out.publishedIso = Str(&root, "published_iso");
    out.nowIso = Str(&root, "now_iso");

    const JsonValue* header = Obj(&root, "header");
    out.display = Str(header, "display");
    out.mode = NormMode(Str(header, "mode"));
    out.flow = Str(header, "flow");
    out.target = Str(header, "target");
    out.monitor = Int(header, "monitor", -1);
    out.hdr = Bool(header, "is_hdr") || out.mode == "HDR";
    out.luminance = Num(header, "luminance");
    out.gamma = Num(header, "gamma");

    out.phase = Str(&root, "phase");
    out.stage = Str(&root, "stage");
    out.runStatus = Str(&root, "run_status");
    if (const JsonValue* pipe = Obj(&root, "pipeline")) {
        if (const JsonValue* steps = Arr(pipe, "steps")) {
            for (const JsonValue& s : steps->arr) {
                if (s.type != JsonValue::Obj) continue;
                LiveStep st;
                st.key = Str(&s, "key");
                st.label = Str(&s, "label");
                if (st.label.empty()) st.label = st.key;
                const std::string status = Str(&s, "status");
                st.status = status == "current" ? StepStatus::Current : status == "done" ? StepStatus::Done : StepStatus::Upcoming;
                out.steps.push_back(std::move(st));
            }
        }
        out.stepIndex = Int(pipe, "index", 0);
        out.stepTotal = Int(pipe, "total", (int)out.steps.size());
    }
    const JsonValue* counters = Obj(&root, "counters");
    out.patchesDone = Int(counters, "patches_done", 0);
    out.patchesTotal = Int(counters, "patches_total", 0);
    out.reads = Int(counters, "reads", 0);
    out.readsOk = Int(counters, "reads_ok", 0);
    out.readsFailed = Int(counters, "reads_failed", 0);
    const JsonValue* timers = Obj(&root, "timers");
    out.runElapsedS = Num(timers, "run_elapsed_s");
    out.stageElapsedS = Num(timers, "stage_elapsed_s");
    out.etaS = Num(timers, "eta_s");
    out.etaHiS = Num(timers, "eta_hi_s");
    out.sPerPatch = Num(timers, "s_per_patch");

    const JsonValue* live = Obj(&root, "liveness");
    out.light = Str(live, "light");
    out.ageS = Num(live, "age_s");
    out.progressAgeS = Num(live, "progress_age_s");

    if (const JsonValue* r = Obj(&root, "last_read")) {
        LiveRead& lr = out.lastRead;
        lr.present = true;
        lr.ok = Bool(r, "ok");
        lr.neutral = Bool(r, "neutral");
        lr.label = Str(r, "label");
        lr.role = Str(r, "role");
        if (const JsonValue* sig = Arr(r, "signal"))
            for (size_t k = 0; k < 3; k++) lr.signal[k] = NumAt(*sig, k);
        if (const JsonValue* xy = Arr(r, "xy")) { lr.x = NumAt(*xy, 0); lr.y = NumAt(*xy, 1); }
        lr.Y = Num(r, "Y");
        lr.de = Num(r, "de");
        lr.cct = Num(r, "cct");
        lr.duv = Num(r, "duv");
        if (const JsonValue* tgt = Obj(Obj(r, "deltas"), "target")) {
            lr.tx = Num(tgt, "x");
            lr.ty = Num(tgt, "y");
            lr.tY = Num(tgt, "Y");
        }
        lr.swatch = SignalHex(lr.signal);
    }
    if (const JsonValue* ld = Obj(&root, "live_de")) {
        out.liveDe.metric = Str(ld, "scoring");
        if (const JsonValue* m = Obj(Obj(ld, "metrics"), out.liveDe.metric.c_str())) {
            out.liveDe.avg = Num(m, "avg");
            out.liveDe.max = Num(m, "max");
            out.liveDe.last = Num(m, "last");
            out.liveDe.n = Int(m, "n", 0);
        }
    }
    if (const JsonValue* de = Obj(&root, "de")) {
        ScoredDe& s = out.scored;
        s.metric = Str(de, "metric");
        s.phase = Str(de, "phase");
        s.avg = Num(de, "avg");
        s.p95 = Num(de, "p95");
        s.max = Num(de, "max");
        s.white = Num(de, "white");
        s.gray = Num(de, "grayscale");
        s.present = std::isfinite(s.avg);
    }
    if (const JsonValue* warm = Obj(&root, "warmup")) {
        const JsonValue* settled = warm->find("settled");
        out.settled = settled && settled->type == JsonValue::Bool ? (settled->b ? 1 : 0) : -1;
        out.driftChannel = Str(warm, "channel");
        out.driftSlopePct10 = Num(warm, "slope_pct_per_10min");
    }
    if (const JsonValue* seam = Obj(&root, "seam")) {
        SeamPacket& p = out.seam;
        p.key = Str(seam, "key");
        p.stage = Str(seam, "stage");
        p.status = Str(seam, "status");
        p.question = Str(seam, "question");
        p.choice = Str(seam, "choice");
        p.note = Str(seam, "note");
        p.recommendation = Str(seam, "recommendation");
        p.options = Strings(Arr(seam, "options"));
        p.checklist = Strings(Arr(seam, "checklist"));
    }
    if (const JsonValue* feed = Arr(&root, "feed")) {
        for (const JsonValue& f : feed->arr) {
            if (f.type != JsonValue::Obj) continue;
            FeedRow row;
            row.time = Str(&f, "time");
            row.level = Str(&f, "level");
            row.stage = Str(&f, "stage");
            row.event = Str(&f, "event");
            row.title = Str(&f, "title");
            row.text = Str(&f, "text");
            out.feed.push_back(std::move(row));
        }
    }
    out.present = true;
    return true;
}

bool ParseLiveCharts(const std::string& json, LiveCharts& out) {
    out = {};
    JsonValue root;
    if (!ParseJson(json, root)) return false;
    if (Int(&root, "live_schema", 0) <= 0) return false;
    const double seq = Num(&root, "seq");
    out.seq = std::isfinite(seq) && seq > 0 ? (uint64_t)seq : 0;
    out.stage = Str(&root, "stage");
    out.targetDuv = Num(&root, "target_duv");

    if (const JsonValue* cie = Obj(&root, "cie")) {
        out.gamutLabel = Str(cie, "gamut_label");
        Primaries(Obj(cie, "primaries"), out.primaries);
        out.hasMeasuredPrimaries = Primaries(Obj(cie, "measured"), out.measuredPrimaries);
        Xy(Arr(cie, "white"), out.white);
        if (const JsonValue* pts = Arr(cie, "points")) {
            out.cie.reserve(pts->arr.size());
            for (const JsonValue& p : pts->arr) {
                if (p.type != JsonValue::Obj) continue;
                const double x = Num(&p, "x"), y = Num(&p, "y");
                if (!std::isfinite(x) || !std::isfinite(y)) continue;
                CiePoint c{ F(x), F(y), F(Num(&p, "tx")), F(Num(&p, "ty")), F(Num(&p, "de")),
                            Bool(&p, "carried"), Bool(&p, "neutral"), Str(&p, "label") };
                c.Y = F(Num(&p, "Y"));
                c.tY = F(Num(&p, "tY"));
                c.L = F(Num(&p, "L"));
                c.C = F(Num(&p, "C"));
                c.H = F(Num(&p, "H"));
                c.tw = F(Num(&p, "tw"));
                c.tm = F(Num(&p, "tm"));
                c.dY = F(Num(&p, "dY"));
                c.jnd = F(Num(&p, "jnd"));
                c.w = F(Num(&p, "w"));
                c.zone = ZoneOf(Str(&p, "zone"));
                c.liftIntended = Hex(Str(&p, "lsc"));
                c.liftMeasured = Hex(Str(&p, "lmc"));
                out.cie.push_back(std::move(c));
            }
        }
    }
    if (const JsonValue* gray = Arr(&root, "grayscale")) {
        for (const JsonValue& g : gray->arr) {
            if (g.type != JsonValue::Obj) continue;
            const double s = Num(&g, "signal");
            if (!std::isfinite(s)) continue;
            GrayRow row{ F(s), F(Num(&g, "Y")), F(Num(&g, "cct")), F(Num(&g, "duv")), F(Num(&g, "de")),
                         F(Num(&g, "r")), F(Num(&g, "g")), F(Num(&g, "b")), Bool(&g, "dim") };
            row.tY = F(Num(&g, "tY"));
            row.dY = F(Num(&g, "dY"));
            row.jnd = F(Num(&g, "jnd"));
            row.tw = F(Num(&g, "tw"));
            row.tm = F(Num(&g, "tm"));
            row.carried = Bool(&g, "carried");
            out.gray.push_back(row);
        }
        std::sort(out.gray.begin(), out.gray.end(), [](const GrayRow& a, const GrayRow& b) { return a.signal < b.signal; });
    }
    if (const JsonValue* eotf = Obj(&root, "eotf")) {
        out.eotfKind = Str(eotf, "kind");
        out.hdr = out.eotfKind == "pq";
        out.gamma = Num(eotf, "gamma");
        out.luminance = Num(eotf, "luminance");
        if (const JsonValue* ref = Arr(eotf, "reference")) {
            for (const JsonValue& r : ref->arr) {
                const double s = NumAt(r, 0), y = NumAt(r, 1);
                if (std::isfinite(s) && std::isfinite(y)) out.eotfReference.push_back({ F(s), F(y) });
            }
        }
        if (const JsonValue* pts = Arr(eotf, "points")) {
            for (const JsonValue& p : pts->arr) {
                const double s = Num(&p, "signal"), y = Num(&p, "Y");
                if (std::isfinite(s) && std::isfinite(y)) out.eotfPoints.push_back({ F(s), F(y) });
            }
            std::sort(out.eotfPoints.begin(), out.eotfPoints.end(), [](const CurvePoint& a, const CurvePoint& b) { return a.x < b.x; });
        }
    }
    if (const JsonValue* conv = Arr(&root, "convergence")) {
        for (const JsonValue& c : conv->arr) {
            if (c.type != JsonValue::Obj) continue;
            out.convergence.push_back({ F(Num(&c, "elapsed_s")), F(Num(&c, "avg")), F(Num(&c, "max")),
                                        Str(&c, "kind") == "scored" });
        }
    }
    if (const JsonValue* off = Arr(&root, "offenders")) {
        for (const JsonValue& o : off->arr) {
            if (o.type != JsonValue::Obj) continue;
            const JsonValue* oog = o.find("oog");
            out.offenders.push_back({ Str(&o, "label"), F(Num(&o, "de")), Hex(Str(&o, "sc")), Hex(Str(&o, "mc")),
                                      oog && ((oog->type == JsonValue::Bool && oog->b) || oog->type == JsonValue::Str) });
        }
    }
    if (const JsonValue* drift = Arr(&root, "channel_drift")) {
        for (const JsonValue& d : drift->arr) {
            if (d.type != JsonValue::Obj) continue;
            const double e = Num(&d, "elapsed_s");
            if (!std::isfinite(e)) continue;
            out.drift.push_back({ F(e), F(Num(&d, "r")), F(Num(&d, "g")), F(Num(&d, "b")), Int(&d, "seg", 0) });
        }
    }
    if (const JsonValue* pr = Obj(&root, "practical")) ParsePractical(*pr, out.practical);
    if (const JsonValue* stages = Arr(&root, "stages_practical")) {
        for (const JsonValue& s : stages->arr) {
            if (s.type != JsonValue::Obj) continue;
            StagePractical sp{ Str(&s, "stage"), F(Num(&s, "score")), Int(&s, "n", 0), {} };
            if (const JsonValue* b = Arr(&s, "bands"))
                for (size_t i = 0; i < b->arr.size(); i++) sp.bands.push_back(F(NumAt(*b, i)));
            out.stages.push_back(std::move(sp));
        }
    }
    out.present = true;
    return true;
}

RunPhase DeriveRunPhase(const ActivePointer& active, const LiveState* state, bool processAlive,
                        double stateAgeS, bool pauseRequested) {
    const std::string& as = active.state;
    if (state && state->present) {
        if (state->light == "done") return RunPhase::Done;
        if (state->light == "paused" || state->seam.Pending()) return RunPhase::Waiting;
        const bool pidKnown = state->pid != 0 || active.pid != 0;
        if (pidKnown && !processAlive) return as == "paused_seam" ? RunPhase::Waiting : RunPhase::Failed;
        if (std::isfinite(stateAgeS) && stateAgeS > kNoDataAfterS) return RunPhase::NoData;
        if (pauseRequested) return RunPhase::Paused;
        if (state->light == "stalled") return RunPhase::Stalled;
        return RunPhase::Running;
    }
    if (!active.present) return RunPhase::None;
    if (as == "done" || as == "cancelled") return RunPhase::Done;
    if (as == "failed") return RunPhase::Failed;
    if (as == "paused_seam") return RunPhase::Waiting;
    if (active.pid != 0) return processAlive ? RunPhase::NoData : RunPhase::Failed;
    return RunPhase::None;   // an older DLC: no live files, no pid — nothing to say
}

std::shared_ptr<const LiveCharts> ChartsFromPatches(const std::vector<Patch>& patches, bool hdr) {
    auto out = std::make_shared<LiveCharts>();
    out->hdr = hdr;
    out->eotfKind = hdr ? "pq" : "power";
    out->gamutLabel = hdr ? "Rec.2020" : "Rec.709 / sRGB";
    static const float kSrgb[3][2] = { { 0.64f, 0.33f }, { 0.30f, 0.60f }, { 0.15f, 0.06f } };
    static const float k2020[3][2] = { { 0.708f, 0.292f }, { 0.170f, 0.797f }, { 0.131f, 0.046f } };
    for (int i = 0; i < 3; i++)
        for (int k = 0; k < 2; k++) out->primaries[i][k] = hdr ? k2020[i][k] : kSrgb[i][k];
    auto xy = [](const double v[3], float& x, float& y) {
        const double s = v[0] + v[1] + v[2];
        if (!(std::isfinite(s) && s > 0)) return false;
        x = (float)(v[0] / s);
        y = (float)(v[1] / s);
        return true;
    };
    std::vector<std::pair<float, const Patch*>> worst;
    for (const Patch& p : patches) {
        float x, y, tx = (float)kNaN, ty = (float)kNaN;
        if (!xy(p.measured, x, y)) continue;
        xy(p.target, tx, ty);
        out->cie.push_back({ x, y, tx, ty, (float)p.de, false, p.grayscale, std::string() });
        if (p.grayscale && std::isfinite(p.measured[1])) {
            out->eotfPoints.push_back({ p.rgb[0], (float)p.measured[1] });
            if (std::isfinite(p.target[1])) out->eotfReference.push_back({ p.rgb[0], (float)p.target[1] });   // nits until below
        }
        if (std::isfinite(p.de)) worst.push_back({ (float)p.de, &p });
    }
    auto bySignal = [](const CurvePoint& a, const CurvePoint& b) { return a.x < b.x; };
    std::sort(out->eotfPoints.begin(), out->eotfPoints.end(), bySignal);
    std::sort(out->eotfReference.begin(), out->eotfReference.end(), bySignal);
    // The reference relative to the target peak, as DLC publishes it.
    float peak = 0.0f;
    for (const CurvePoint& q : out->eotfReference) peak = (std::max)(peak, q.y);
    if (peak > 0.0f) {
        out->luminance = peak;
        for (CurvePoint& q : out->eotfReference) q.y /= peak;
    }
    std::sort(worst.begin(), worst.end(), [](const auto& a, const auto& b) { return a.first > b.first; });
    for (size_t i = 0; i < worst.size() && i < 12; i++) {
        const Patch& p = *worst[i].second;
        const double s[3] = { p.rgb[0], p.rgb[1], p.rgb[2] };
        char label[48];
        snprintf(label, sizeof(label), "%.2f %.2f %.2f", p.rgb[0], p.rgb[1], p.rgb[2]);
        out->offenders.push_back({ label, worst[i].first, SignalHex(s), 0u, p.clamped });
    }
    out->present = !out->cie.empty();
    return out;
}

// ---- control ---------------------------------------------------------------------------------------

namespace {

std::string LocalIsoNow() {
    SYSTEMTIME st;
    GetLocalTime(&st);
    char buf[32];
    snprintf(buf, sizeof(buf), "%04u-%02u-%02uT%02u:%02u:%02u", st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
    return buf;
}

std::string LastErrorText(const char* what) {
    char buf[96];
    snprintf(buf, sizeof(buf), "%s failed (error %lu)", what, GetLastError());
    return buf;
}

}  // namespace

bool WriteRunControl(const std::wstring& runDir, RunControl action, std::string* error) {
    if (runDir.empty()) {
        if (error) *error = "no run";
        return false;
    }
    std::string json = "{\"action\":\"";
    json += action == RunControl::Pause ? "pause" : action == RunControl::Resume ? "resume" : "cancel";
    json += "\",\"requested\":\"" + LocalIsoNow() + "\"";
    if (action == RunControl::Pause) json += ",\"timeout_s\":180,\"on_timeout\":\"rollback\"";
    json += ",\"source\":\"desktoplut\"}";

    const std::wstring dst = runDir + L"\\control.json";
    const std::wstring tmp = runDir + L"\\.control.json." + std::to_wstring(GetCurrentProcessId()) + L".tmp";
    HANDLE h = CreateFileW(tmp.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        if (error) *error = LastErrorText("create");
        return false;
    }
    DWORD written = 0;
    const bool ok = WriteFile(h, json.data(), (DWORD)json.size(), &written, nullptr) && written == json.size() &&
                    FlushFileBuffers(h);
    CloseHandle(h);
    if (!ok || !MoveFileExW(tmp.c_str(), dst.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        if (error) *error = LastErrorText(ok ? "replace" : "write");
        DeleteFileW(tmp.c_str());
        return false;
    }
    return true;
}

// ---- the watcher -----------------------------------------------------------------------------------

namespace {

struct Stamp {
    uint64_t size = 0, mtime = 0;
    bool exists = false;
    bool operator==(const Stamp& o) const { return size == o.size && mtime == o.mtime && exists == o.exists; }
    bool operator!=(const Stamp& o) const { return !(*this == o); }
};

Stamp StatFile(const std::wstring& path) {
    Stamp s;
    WIN32_FILE_ATTRIBUTE_DATA a;
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &a) || (a.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
        return s;
    s.exists = true;
    s.size = ((uint64_t)a.nFileSizeHigh << 32) | a.nFileSizeLow;
    s.mtime = ((uint64_t)a.ftLastWriteTime.dwHighDateTime << 32) | a.ftLastWriteTime.dwLowDateTime;
    return s;
}

bool ReadAll(const std::wstring& path, std::string& out, uint64_t maxBytes) {
    out.clear();
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER size = {};
    bool ok = GetFileSizeEx(h, &size) != 0 && (uint64_t)size.QuadPart <= maxBytes;
    if (ok && size.QuadPart > 0) {
        out.resize((size_t)size.QuadPart);
        DWORD got = 0;
        ok = ReadFile(h, out.data(), (DWORD)out.size(), &got, nullptr) != 0;
        out.resize(got);
    }
    CloseHandle(h);
    return ok;
}

uint64_t NowFileTime() {
    FILETIME ft;
    GetSystemTimeAsFileTime(&ft);
    return ((uint64_t)ft.dwHighDateTime << 32) | ft.dwLowDateTime;
}

bool ProcessAlive(uint32_t pid) {
    if (!pid) return false;
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!h) return GetLastError() == ERROR_ACCESS_DENIED;   // exists, just not ours to query
    DWORD code = 0;
    const bool alive = GetExitCodeProcess(h, &code) && code == STILL_ACTIVE;
    CloseHandle(h);
    return alive;
}

std::mutex g_mutex;
std::condition_variable g_cv;
std::thread g_thread;
bool g_stop = false;
bool g_active = false;
bool g_kick = false;
std::wstring g_folder;
std::shared_ptr<const Live> g_live = std::make_shared<Live>();
std::function<void()> g_listener;
std::deque<std::wstring> g_chartQueue;
std::map<std::wstring, std::shared_ptr<const LiveCharts>> g_runCharts;   // NormPath(run dir) -> final charts

struct WatchCache {
    std::wstring folder, run;
    Stamp activeStamp, stateStamp, chartsStamp, controlStamp;
    ActivePointer active;
    std::shared_ptr<const LiveState> state;
    std::shared_ptr<const LiveCharts> charts;
    bool pause = false, cancel = false;
    std::string pauseIso;
};

bool SameLive(const Live& a, const Live& b) {
    return a.dlcFolder == b.dlcFolder && a.active.run == b.active.run && a.active.state == b.active.state &&
           a.state == b.state && a.charts == b.charts && a.phase == b.phase && a.processAlive == b.processAlive &&
           a.pauseRequested == b.pauseRequested && a.cancelRequested == b.cancelRequested;
}

// One poll. Returns the snapshot to publish; `live` tells the loop to poll fast.
std::shared_ptr<const Live> Poll(WatchCache& c, const std::wstring& folder, bool& live) {
    if (c.folder != folder) c = WatchCache{ folder };
    auto out = std::make_shared<Live>();
    out->dlcFolder = folder;
    live = false;
    if (folder.empty()) return out;

    const std::wstring activePath = folder + L"\\runs\\active.json";
    const Stamp as = StatFile(activePath);
    if (as != c.activeStamp) {
        c.activeStamp = as;
        std::string text;
        ActivePointer p;
        if (as.exists && ReadAll(activePath, text, kSmallBytes) && ParseActivePointer(text, p)) c.active = p;
        else if (!as.exists) c.active = {};
        // unreadable mid-rewrite: keep the last pointer
    }
    if (NormPath(c.active.run) != NormPath(c.run)) {
        c.run = c.active.run;
        c.stateStamp = c.chartsStamp = c.controlStamp = {};
        c.state.reset();
        c.charts.reset();
        c.pause = c.cancel = false;
        c.pauseIso.clear();
    }
    out->active = c.active;
    if (c.run.empty()) return out;

    const std::wstring liveDir = c.run + L"\\live";
    const Stamp ss = StatFile(liveDir + L"\\state.json");
    if (ss != c.stateStamp) {
        std::string text;
        auto st = std::make_shared<LiveState>();
        if (ss.exists && ReadAll(liveDir + L"\\state.json", text, kStateBytes) && ParseLiveState(text, *st)) {
            c.state = st;
            c.stateStamp = ss;
        } else if (!ss.exists) {
            c.state.reset();
            c.stateStamp = ss;
        }   // unreadable: retry next poll, keep the last good state
    }
    const Stamp cs = StatFile(liveDir + L"\\charts.json");
    if (cs != c.chartsStamp) {
        std::string text;
        auto ch = std::make_shared<LiveCharts>();
        if (cs.exists && ReadAll(liveDir + L"\\charts.json", text, kChartsBytes) && ParseLiveCharts(text, *ch)) {
            c.charts = ch;
            c.chartsStamp = cs;
        } else if (!cs.exists) {
            c.charts.reset();
            c.chartsStamp = cs;
        }
    }
    const Stamp ctl = StatFile(c.run + L"\\control.json");
    if (ctl != c.controlStamp) {
        c.controlStamp = ctl;
        c.pause = c.cancel = false;
        c.pauseIso.clear();
        std::string text;
        JsonValue root;
        if (ctl.exists && ReadAll(c.run + L"\\control.json", text, kSmallBytes) && ParseJson(text, root)) {
            const std::string action = Str(&root, "action");
            c.pause = action == "pause";
            c.cancel = action == "cancel";
            if (c.pause) c.pauseIso = Str(&root, "requested");
        }
    }
    out->state = c.state;
    out->charts = c.charts;
    out->pauseRequested = c.pause;
    out->cancelRequested = c.cancel;
    out->pauseRequestedIso = c.pauseIso;
    const uint32_t pid = c.active.pid ? c.active.pid : (c.state ? c.state->pid : 0);
    out->processAlive = ProcessAlive(pid);
    if (c.stateStamp.exists) {
        const uint64_t now = NowFileTime();
        out->stateAgeS = now > c.stateStamp.mtime ? (double)(now - c.stateStamp.mtime) / 1e7 : 0.0;
    }
    out->phase = DeriveRunPhase(c.active, c.state.get(), out->processAlive, out->stateAgeS, c.pause);
    live = out->phase == RunPhase::Running || out->phase == RunPhase::Paused || out->phase == RunPhase::Stalled ||
           out->phase == RunPhase::NoData || (out->phase == RunPhase::Waiting && out->processAlive);
    return out;
}

void Notify() {
    std::function<void()> fn;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        fn = g_listener;
    }
    if (fn) fn();
}

void WatchMain() {
    WatchCache cache;
    bool fast = false;
    for (;;) {
        std::wstring folder, chartRun;
        {
            std::unique_lock<std::mutex> lock(g_mutex);
            const auto wait = std::chrono::milliseconds(fast ? 500 : 2000);
            g_cv.wait_for(lock, wait, [] { return g_stop || g_kick || !g_chartQueue.empty(); });
            if (g_stop) return;
            if (!g_chartQueue.empty()) {
                chartRun = g_chartQueue.front();
                g_chartQueue.pop_front();
            }
        }
        if (!chartRun.empty()) {   // a record's charts: one file, then back to the watch
            auto charts = std::make_shared<LiveCharts>();
            std::string text;
            const std::wstring path = chartRun + L"\\live\\charts.json";
            if (!(ReadAll(path, text, kChartsBytes) && ParseLiveCharts(text, *charts))) *charts = LiveCharts();
            {
                std::lock_guard<std::mutex> lock(g_mutex);
                if (g_stop) return;
                g_runCharts[NormPath(chartRun)] = charts;
            }
            Notify();
            continue;
        }
        {
            std::unique_lock<std::mutex> lock(g_mutex);
            if (!g_active && !g_kick) continue;
            if (!g_active) {   // a kick while hidden (folder change): just forget the old run
                g_kick = false;
                continue;
            }
            g_kick = false;
            folder = g_folder;
        }
        std::shared_ptr<const Live> next = Poll(cache, folder, fast);
        bool changed = false;
        {
            std::lock_guard<std::mutex> lock(g_mutex);
            if (g_stop) return;
            if (!SameLive(*g_live, *next) || (next->state && g_live->state && next->state->seq != g_live->state->seq)) {
                g_live = next;
                changed = true;
            }
        }
        if (changed) Notify();
    }
}

void EnsureThreadLocked() {
    if (!g_thread.joinable() && !g_stop) g_thread = std::thread(WatchMain);
}

}  // namespace

void SetLiveListener(std::function<void()> onChange) {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_listener = std::move(onChange);
}

void WatchLive(const std::wstring& dlcFolder) {
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (g_stop) return;
        if (NormPath(dlcFolder) == NormPath(g_folder) && g_thread.joinable()) return;
        g_folder = dlcFolder;
        g_kick = true;
        EnsureThreadLocked();
    }
    g_cv.notify_one();
}

void SetLiveWatchActive(bool active) {
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (g_stop || g_active == active) return;
        g_active = active;
        g_kick = active;   // poll at once on show
    }
    g_cv.notify_one();
}

void StopLiveWatch() {
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        g_stop = true;
        g_listener = nullptr;
    }
    g_cv.notify_one();
    if (g_thread.joinable()) g_thread.join();
}

std::shared_ptr<const LiveCharts> RunCharts(const std::wstring& runDir) {
    if (runDir.empty()) return nullptr;
    const std::wstring key = NormPath(runDir);
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (auto it = g_runCharts.find(key); it != g_runCharts.end()) return it->second;
        if (g_stop) return nullptr;
        for (const std::wstring& q : g_chartQueue) if (NormPath(q) == key) return nullptr;
        g_chartQueue.push_back(runDir);
        EnsureThreadLocked();
    }
    g_cv.notify_one();
    return nullptr;
}

void SeedRunChartsForTest(const std::wstring& runDir, std::shared_ptr<const LiveCharts> charts) {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_runCharts[NormPath(runDir)] = std::move(charts);
}

std::shared_ptr<const Live> CurrentLive() {
    std::lock_guard<std::mutex> lock(g_mutex);
    return g_live;
}

}  // namespace dlc
