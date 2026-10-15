#include "doctest.h"
#include "dlc/dlc_live.h"

#include <windows.h>
#include <cmath>
#include <string>

// ============================================================================
// The DLC run reader (src/dlc/dlc_live.*): runs\active.json, live\state.json + charts.json written by DLC's
// live publisher (dlc/dashboard/publish.py), the run's phase, control.json writes. Synthetic data only.
// ============================================================================

using namespace dlc;

namespace {

struct LiveTempDir {
    std::wstring path;
    LiveTempDir() {
        wchar_t base[MAX_PATH];
        GetTempPathW(MAX_PATH, base);
        path = std::wstring(base) + L"dlut_dlc_live_" + std::to_wstring(GetCurrentProcessId()) + L"_" +
               std::to_wstring(GetTickCount64());
        CreateDirectoryW(path.c_str(), nullptr);
    }
    ~LiveTempDir() {
        DeleteFileW((path + L"\\control.json").c_str());
        RemoveDirectoryW(path.c_str());
    }
};

std::string ReadText(const std::wstring& path) {
    std::string out;
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    if (h == INVALID_HANDLE_VALUE) return out;
    char buf[4096];
    DWORD got = 0;
    while (ReadFile(h, buf, sizeof(buf), &got, nullptr) && got) out.append(buf, got);
    CloseHandle(h);
    return out;
}

const char* kState = R"({
  "live_schema": 1, "seq": 42, "pid": 4242, "published_iso": "2026-10-15T21:42:00", "now_iso": "2026-10-15T21:42:00",
  "run_dir": "D:\\Cal\\DLC\\runs\\20261015_205100_000000_hdr_panel", "run_id": "r1",
  "header": {"flow": "full", "mode": "HDR", "display": "Panel A", "monitor": 0, "target": "rec2020_pq_1000",
             "is_hdr": true, "luminance": 1000.0, "gamma": null},
  "phase": "measure", "stage": "measure:post-mhc", "run_status": "running",
  "pipeline": {"steps": [{"key": "preflight", "label": "Preflight", "status": "done"},
                         {"key": "measure:post-mhc", "label": "Post-MHC", "status": "current"},
                         {"key": "verify", "label": "Verify", "status": "upcoming"}],
               "total": 3, "index": 2, "current": "measure:post-mhc"},
  "counters": {"patches_done": 182, "patches_total": 309, "reads": 190, "reads_ok": 188, "reads_failed": 2},
  "timers": {"run_elapsed_s": 3060.0, "stage_elapsed_s": 900.5, "s_per_patch": 4.9, "eta_s": 1260.0, "eta_hi_s": 1500.0},
  "liveness": {"light": "live", "age_s": 1.2, "progress_age_s": 3.0},
  "last_read": {"label": "p0183", "role": "measurement", "rgb": [0.68, 0.78, 0.88], "signal": [0.68, 0.78, 0.88],
                "Y": 409.8, "xy": [0.2901, 0.3012], "ok": true, "neutral": false, "de": 0.84,
                "deltas": {"target": {"x": 0.2899, "y": 0.3010, "Y": 412.6}}},
  "live_de": {"scoring": "itp", "n": 182, "metrics": {"itp": {"avg": 0.62, "max": 2.1, "last": 0.84, "n": 182}}},
  "de": {"metric": "dE_ITP", "phase": "verification", "avg": NaN, "p95": null},
  "seam": {"key": "brightness:adjust", "stage": "brightness", "status": "paused", "question": "White 152 nits",
           "options": ["accept", "abort"], "recommendation": "accept", "checklist": ["Turn the OSD", {"text": "Re-read"}]},
  "feed": [{"time": "2026-10-15T21:03:00", "level": "INFO", "stage": "brightness", "event": "seam",
            "title": "brightness:adjust", "text": "decided"},
           {"time": "2026-10-15T21:40:00", "level": "INFO", "stage": "measure", "event": "supervisor_note",
            "title": "claude", "text": "Red max is the panel floor."}]
})";

const char* kCharts = R"({
  "live_schema": 1, "seq": 43, "stage": "measure:post-mhc", "target_duv": 0.0032,
  "cie": {"points": [{"x": 0.30, "y": 0.32, "tx": 0.31, "ty": 0.33, "de": 0.8, "label": "p1", "neutral": true},
                     {"x": 0.64, "y": 0.33, "tx": 0.64, "ty": 0.33, "de": 3.5, "carried": true},
                     {"x": null, "y": 0.1}],
          "white": [0.3127, 0.329], "primaries": {"r": [0.708, 0.292], "g": [0.17, 0.797], "b": [0.131, 0.046]},
          "gamut_label": "Rec.2020", "measured": {"r": [0.68, 0.31], "g": [0.26, 0.69], "b": [0.15, 0.05]}},
  "grayscale": [{"signal": 0.5, "Y": 92.0, "cct": 6500, "duv": 0.003, "de": 0.4, "r": 0.2, "g": -0.1, "b": -0.1},
                {"signal": 0.1, "Y": 0.3, "cct": 7000, "duv": 0.004, "de": 1.9, "r": -4.0, "g": -1.0, "b": 5.0, "dim": true}],
  "eotf": {"kind": "pq", "gamma": 2.2, "luminance": 1000, "reference": [[0.0, 0.0], [0.5, 92.2], [0.75, 983]],
           "points": [{"signal": 0.75, "Y": 980}, {"signal": 0.5, "Y": 92.0}]},
  "convergence": [{"elapsed_s": 100, "avg": 1.2, "max": 4.0, "kind": "build"}, {"elapsed_s": 900, "avg": 0.6, "max": 2.0, "kind": "scored"}],
  "offenders": [{"label": "p0125", "de": 12.5, "sc": "#311406", "mc": "#200a08", "oog": true},
                {"label": "p0005", "de": 3.2, "sc": "#cfcfcf", "mc": "#ffffff", "oog": null}]
})";

}  // namespace

TEST_CASE("dlc live: active.json pointer, old and new forms") {
    ActivePointer a;
    REQUIRE(ParseActivePointer(R"({"run": "D:\\Cal\\runs\\r1", "events": "D:\\Cal\\runs\\r1\\events.jsonl",
                                   "flow": "full", "updated": "2026-10-15T20:51:00"})", a));
    CHECK(a.run == L"D:\\Cal\\runs\\r1");
    CHECK(a.pid == 0);
    CHECK(a.state.empty());
    REQUIRE(ParseActivePointer(R"({"events": "D:\\Cal\\runs\\r2\\events.jsonl", "pid": 4242, "state": "paused_seam",
                                   "mode": "hdr", "monitor": 1, "hardware_id": "ABC1234", "driver": "claude"})", a));
    CHECK(a.run == L"D:\\Cal\\runs\\r2");   // from the events path
    CHECK(a.pid == 4242);
    CHECK(a.state == "paused_seam");
    CHECK(a.mode == "HDR");
    CHECK(a.monitor == 1);
    CHECK(a.driver == "claude");
    CHECK_FALSE(ParseActivePointer("{}", a));
    CHECK_FALSE(ParseActivePointer("not json", a));
}

TEST_CASE("dlc live: state.json") {
    LiveState s;
    REQUIRE(ParseLiveState(kState, s));
    CHECK(s.schema == 1);
    CHECK(s.seq == 42);
    CHECK(s.pid == 4242);
    CHECK(s.runDir == L"D:\\Cal\\DLC\\runs\\20261015_205100_000000_hdr_panel");
    CHECK(s.display == "Panel A");
    CHECK(s.mode == "HDR");
    CHECK(s.hdr);
    CHECK(s.flow == "full");
    REQUIRE(s.steps.size() == 3);
    CHECK(s.steps[0].status == StepStatus::Done);
    CHECK(s.steps[1].status == StepStatus::Current);
    CHECK(s.steps[1].label == "Post-MHC");
    CHECK(s.stepIndex == 2);
    CHECK(s.patchesDone == 182);
    CHECK(s.readsFailed == 2);
    CHECK(s.etaS == doctest::Approx(1260.0));
    CHECK(s.light == "live");
    CHECK(s.lastRead.present);
    CHECK(s.lastRead.de == doctest::Approx(0.84));
    CHECK(s.lastRead.tY == doctest::Approx(412.6));
    CHECK(s.lastRead.swatch == 0xADC7E0u);   // 0.68 0.78 0.88 as 8-bit codes
    CHECK(s.liveDe.metric == "itp");
    CHECK(s.liveDe.max == doctest::Approx(2.1));
    CHECK_FALSE(s.scored.present);             // avg NaN: no verdict yet
    CHECK(s.seam.Pending());
    CHECK(s.seam.recommendation == "accept");
    REQUIRE(s.seam.checklist.size() == 2);
    CHECK(s.seam.checklist[1] == "Re-read");
    REQUIRE(s.feed.size() == 2);
    CHECK(s.feed[1].event == "supervisor_note");
    // An unknown or missing schema is not a live state.
    CHECK_FALSE(ParseLiveState(R"({"header": {}})", s));
}

TEST_CASE("dlc live: charts.json") {
    LiveCharts c;
    REQUIRE(ParseLiveCharts(kCharts, c));
    CHECK(c.present);
    CHECK(c.hdr);
    REQUIRE(c.cie.size() == 2);                // the point without x is dropped
    CHECK(c.cie[0].label == "p1");
    CHECK(c.cie[1].carried);
    CHECK(c.hasMeasuredPrimaries);
    CHECK(c.primaries[1][1] == doctest::Approx(0.797f));
    CHECK(c.gamutLabel == "Rec.2020");
    REQUIRE(c.gray.size() == 2);
    CHECK(c.gray[0].signal == doctest::Approx(0.1f));   // sorted by signal
    CHECK(c.gray[0].dim);
    REQUIRE(c.eotfPoints.size() == 2);
    CHECK(c.eotfPoints[0].x == doctest::Approx(0.5f));  // sorted by signal
    CHECK(c.eotfReference.size() == 3);
    REQUIRE(c.convergence.size() == 2);
    CHECK(c.convergence[1].scored);
    REQUIRE(c.offenders.size() == 2);
    CHECK(c.offenders[0].intended == 0x311406u);
    CHECK(c.offenders[0].oog);
    CHECK_FALSE(c.offenders[1].oog);
}

TEST_CASE("dlc live: the run's phase") {
    ActivePointer a;
    a.present = true;
    a.run = L"D:\\runs\\r1";
    LiveState s;
    REQUIRE(ParseLiveState(kState, s));
    s.seam.status = "decided";
    // Alive and fresh: running; a requested pause; DLC's own stall verdict.
    CHECK(DeriveRunPhase(a, &s, true, 1.0, false) == RunPhase::Running);
    CHECK(DeriveRunPhase(a, &s, true, 1.0, true) == RunPhase::Paused);
    s.light = "stalled";
    CHECK(DeriveRunPhase(a, &s, true, 1.0, false) == RunPhase::Stalled);
    s.light = "live";
    // The process runs but the files stopped: the publisher, not the run, is what we know is wrong.
    CHECK(DeriveRunPhase(a, &s, true, 60.0, false) == RunPhase::NoData);
    // The process is gone without finishing: failed.
    CHECK(DeriveRunPhase(a, &s, false, 60.0, false) == RunPhase::Failed);
    // A seam: the process exits on purpose.
    s.seam.status = "paused";
    CHECK(DeriveRunPhase(a, &s, false, 600.0, false) == RunPhase::Waiting);
    s.seam.status = "decided";
    a.state = "paused_seam";
    CHECK(DeriveRunPhase(a, &s, false, 600.0, false) == RunPhase::Waiting);
    // Finished.
    s.light = "done";
    CHECK(DeriveRunPhase(a, &s, false, 600.0, false) == RunPhase::Done);
    // No live files: what the pointer says, else nothing (an older DLC).
    ActivePointer old;
    old.present = true;
    old.run = L"D:\\runs\\r0";
    CHECK(DeriveRunPhase(old, nullptr, false, NAN, false) == RunPhase::None);
    old.pid = 99;
    CHECK(DeriveRunPhase(old, nullptr, true, NAN, false) == RunPhase::NoData);
    CHECK(DeriveRunPhase(old, nullptr, false, NAN, false) == RunPhase::Failed);
    old.state = "done";
    CHECK(DeriveRunPhase(old, nullptr, false, NAN, false) == RunPhase::Done);
    CHECK(DeriveRunPhase(ActivePointer(), nullptr, false, NAN, false) == RunPhase::None);
}

TEST_CASE("dlc live: control.json is written whole, in DLC's format") {
    LiveTempDir t;
    std::string err;
    REQUIRE(WriteRunControl(t.path, RunControl::Pause, &err));
    std::string text = ReadText(t.path + L"\\control.json");
    CHECK(text.find("\"action\":\"pause\"") != std::string::npos);
    CHECK(text.find("\"timeout_s\":180") != std::string::npos);
    CHECK(text.find("\"on_timeout\":\"rollback\"") != std::string::npos);
    REQUIRE(WriteRunControl(t.path, RunControl::Cancel, &err));   // replaces the pause
    text = ReadText(t.path + L"\\control.json");
    CHECK(text.find("\"action\":\"cancel\"") != std::string::npos);
    CHECK(text.find("pause") == std::string::npos);
    CHECK_FALSE(WriteRunControl(L"", RunControl::Cancel, &err));
    CHECK_FALSE(WriteRunControl(t.path + L"\\missing\\dir", RunControl::Resume, &err));
    CHECK_FALSE(err.empty());
}

TEST_CASE("dlc live: a real run's published files parse (DLUT_LIVE_RUN)") {
    wchar_t dir[MAX_PATH] = {};
    if (GetEnvironmentVariableW(L"DLUT_LIVE_RUN", dir, MAX_PATH) == 0) return;   // opt-in: a run folder with live\*
    LiveState s;
    REQUIRE(ParseLiveState(ReadText(std::wstring(dir) + L"\\live\\state.json"), s));
    CHECK(s.schema >= 1);
    CHECK_FALSE(s.steps.empty());
    CHECK_FALSE(s.display.empty());
    CHECK_FALSE(s.feed.empty());
    LiveCharts c;
    REQUIRE(ParseLiveCharts(ReadText(std::wstring(dir) + L"\\live\\charts.json"), c));
    CHECK_FALSE(c.cie.empty());
    MESSAGE("state: ", s.display, " ", s.mode, " ", s.flow, " light=", s.light, " steps=", s.steps.size(),
            " feed=", s.feed.size(), "; charts: cie=", c.cie.size(), " gray=", c.gray.size(), " eotf=", c.eotfPoints.size(),
            " offenders=", c.offenders.size());
}

TEST_CASE("dlc live: charts from a report's patch metrics") {
    std::vector<Patch> patches(3);
    patches[0].rgb[0] = patches[0].rgb[1] = patches[0].rgb[2] = 0.5f;
    patches[0].grayscale = true;
    patches[0].measured[0] = 20.0; patches[0].measured[1] = 21.0; patches[0].measured[2] = 23.0;
    patches[0].target[0] = 20.3; patches[0].target[1] = 21.4; patches[0].target[2] = 23.2;
    patches[0].de = 0.4;
    patches[1].rgb[0] = 1.0f;
    patches[1].measured[0] = 41.0; patches[1].measured[1] = 21.0; patches[1].measured[2] = 2.0;
    patches[1].target[0] = 41.2; patches[1].target[1] = 21.3; patches[1].target[2] = 1.9;
    patches[1].de = 2.5;
    // A patch without a reading is skipped.
    auto c = ChartsFromPatches(patches, false);
    REQUIRE(static_cast<bool>(c));
    CHECK(c->present);
    CHECK_FALSE(c->hdr);
    REQUIRE(c->cie.size() == 2);
    CHECK(c->cie[0].x == doctest::Approx(20.0 / 64.0));
    CHECK(c->eotfPoints.size() == 1);
    REQUIRE(c->eotfReference.size() == 1);
    CHECK(c->eotfReference[0].y == doctest::Approx(1.0f));   // relative to the peak, as DLC publishes it
    CHECK(c->luminance == doctest::Approx(21.4));
    REQUIRE(c->offenders.size() == 2);
    CHECK(c->offenders[0].de == doctest::Approx(2.5f));   // worst first
    CHECK(c->primaries[0][0] == doctest::Approx(0.64f));
    CHECK_FALSE(ChartsFromPatches({}, true)->present);
}

TEST_CASE("dlc live: the practical view (bands, hues, misses, limits, drift, stages, warm-up)") {
    const char* charts = R"({
  "live_schema": 1, "seq": 7, "stage": "measure:verify",
  "cie": {"points": [{"x": 0.30, "y": 0.32, "tx": 0.31, "ty": 0.33, "de": 0.8, "Y": 121.4, "tY": 122.7, "L": -0.81,
                      "C": -0.04, "H": -0.08, "tw": 0.08, "tm": 0.03, "dY": -1.09, "jnd": 1.37, "w": 0.0026,
                      "zone": "limits", "lsc": "#e5e9eb", "lmc": "#d7e7eb"}]},
  "grayscale": [{"signal": 0.05, "Y": 0.055, "cct": 7043, "duv": 0.004, "de": 1.88, "r": -4, "g": -1, "b": 5,
                 "tY": 0.0496, "dY": 11.1, "jnd": 6.9, "tw": -0.55, "tm": -0.82, "carried": true}],
  "channel_drift": [{"elapsed_s": 140.7, "r": 0.0, "g": 0.0, "b": 0.0, "seg": 0},
                    {"elapsed_s": 237.7, "r": -0.1, "g": -0.17, "b": -0.15, "seg": 1}, {"r": 1}],
  "practical": {"weighting": "content", "score": 1.175, "coverage": 1.0, "offset_L": -0.536, "n": 451,
                "bands": [{"label": "<1", "lo": 0.0, "hi": 1.0, "share": 0.3845, "n": 82, "mean": 1.217, "p95": 2.963,
                           "L": -0.2, "Lr": 0.6, "C": 0.83, "H": 1.276},
                          {"label": ">203", "lo": 203.0, "hi": null, "share": 0.02, "n": 9, "mean": 2.4, "p95": 4.1,
                           "L": -0.8, "C": 1.5, "H": 1.1}],
                "hues": [{"sector": 1, "name": "orange", "n": 131, "w": 0.2968, "de": 1.472, "C": 0.358, "H": -0.694},
                         {"sector": 5, "name": "spring", "n": 0}],
                "worst": [{"desc": "0.064 nit \u00b7 grey", "de": 1.917, "impact": 0.0244, "w": 0.0127, "comp": "L",
                           "zone": "core", "dY": 8.9, "sc": "#ebe2eb", "mc": "#e2d5eb", "label": "p0262"}],
                "worst_apart": [{"desc": "1726 nit \u00b7 grey", "de": 14.02, "impact": 0.0, "w": 0.0, "comp": "L",
                                 "zone": "anchor", "dY": -14.4, "sc": "#ebebeb", "mc": "#ebdcdc", "label": "p0005"}],
                "limits": {"peak": 1476.2, "target_peak": 1729.3, "neutral_peak": 397.6, "black": 0.00235,
                           "white": {"Y": 200.3, "tY": 202.9, "de": 1.015, "cct": 6499.0, "duv": 0.0035, "tw": 0.21,
                                     "tm": -0.11, "dY": -1.28}},
                "content": {"class": "seq_hdr_balanced", "score": 1.153}},
  "stages_practical": [{"stage": "measure:baseline", "score": 2.4, "n": 309, "bands": [3.4, null, 1.6]},
                       {"stage": "measure:verify", "score": 1.175, "n": 451, "bands": [1.2, 1.1, 0.9]}]
})";
    LiveCharts c;
    REQUIRE(ParseLiveCharts(charts, c));
    REQUIRE(c.cie.size() == 1);
    const CiePoint& p = c.cie[0];
    CHECK(p.tY == doctest::Approx(122.7f));
    CHECK(p.L == doctest::Approx(-0.81f));
    CHECK(p.jnd == doctest::Approx(1.37f));
    CHECK(p.w == doctest::Approx(0.0026f));
    CHECK(p.zone == Zone::Limits);
    CHECK(p.liftIntended == 0xE5E9EBu);
    REQUIRE(c.gray.size() == 1);
    CHECK(c.gray[0].dY == doctest::Approx(11.1f));
    CHECK(c.gray[0].tw == doctest::Approx(-0.55f));
    CHECK(c.gray[0].carried);
    REQUIRE(c.drift.size() == 2);   // a check without its time is dropped
    CHECK(c.drift[1].seg == 1);
    CHECK(c.drift[1].g == doctest::Approx(-0.17f));
    const Practical& pr = c.practical;
    REQUIRE(pr.present);
    CHECK(pr.contentWeighted);
    CHECK(pr.score == doctest::Approx(1.175f));
    CHECK(pr.offsetL == doctest::Approx(-0.536f));
    REQUIRE(pr.bands.size() == 2);
    CHECK(pr.bands[0].Lr == doctest::Approx(0.6f));
    CHECK(std::isnan(pr.bands[1].Lr));           // an older file: no RMS lightness
    CHECK(std::isinf(pr.bands[1].hi));            // the open top band
    REQUIRE(pr.hues.size() == 2);
    CHECK(pr.hues[0].name == "orange");
    CHECK(pr.hues[1].n == 0);
    REQUIRE(pr.worst.size() == 1);
    CHECK(pr.worst[0].comp == 'L');
    CHECK(pr.worst[0].zone == Zone::Core);
    CHECK(pr.worst[0].intended == 0xEBE2EBu);
    REQUIRE(pr.apart.size() == 1);
    CHECK(pr.apart[0].zone == Zone::Anchor);
    CHECK(pr.limits.neutralPeak == doctest::Approx(397.6f));
    CHECK(pr.limits.hasWhite);
    CHECK(pr.limits.whiteCct == doctest::Approx(6499.0f));
    CHECK(pr.contentClass == "seq_hdr_balanced");
    CHECK(pr.contentScore == doctest::Approx(1.153f));
    REQUIRE(c.stages.size() == 2);
    CHECK(c.stages[0].bands.size() == 3);
    CHECK(std::isnan(c.stages[0].bands[1]));

    LiveState s;
    REQUIRE(ParseLiveState(R"({"live_schema": 1, "seq": 1, "warmup": {"settled": false, "channel": "b",
                                 "slope_pct_per_10min": 0.42}})", s));
    CHECK(s.settled == 0);
    CHECK(s.driftChannel == "b");
    CHECK(s.driftSlopePct10 == doctest::Approx(0.42));
    REQUIRE(ParseLiveState(R"({"live_schema": 1, "seq": 1})", s));
    CHECK(s.settled == -1);

    LiveCharts old;   // a file from before the practical view: nothing claimed
    REQUIRE(ParseLiveCharts(R"({"live_schema": 1, "seq": 1})", old));
    CHECK_FALSE(old.practical.present);
    CHECK(old.stages.empty());
}
