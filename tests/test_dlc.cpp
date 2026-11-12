#include "doctest.h"
#include "dlc/dlc_data.h"
#include "dlc/dlc_join.h"
#include "dlc/dlc_live.h"
#include "dlc/dlc_scan.h"
#include "dlc/dlc_yaml.h"
#include "ipc_json.h"

#include <windows.h>
#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>

// ============================================================================
// DLC records (src/dlc/*): tolerant readers of what DLC leaves on disk, the display/record join and the
// folder scan. Synthetic data only: no real display names, folders or monitor indices.
// ============================================================================

using namespace dlc;

namespace {

struct TempDir {
    std::wstring path;
    TempDir() {
        wchar_t base[MAX_PATH];
        GetTempPathW(MAX_PATH, base);
        path = std::wstring(base) + L"dlut_dlc_test_" + std::to_wstring(GetCurrentProcessId()) + L"_" +
               std::to_wstring(GetTickCount64());
        CreateDirectoryW(path.c_str(), nullptr);
    }
    ~TempDir() { Remove(path); }
    static void Remove(const std::wstring& dir) {
        WIN32_FIND_DATAW fd;
        HANDLE h = FindFirstFileW((dir + L"\\*").c_str(), &fd);
        if (h != INVALID_HANDLE_VALUE) {
            do {
                const std::wstring n = fd.cFileName;
                if (n == L"." || n == L"..") continue;
                const std::wstring p = dir + L"\\" + n;
                if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) Remove(p);
                else DeleteFileW(p.c_str());
            } while (FindNextFileW(h, &fd));
            FindClose(h);
        }
        RemoveDirectoryW(dir.c_str());
    }
    std::wstring mkdir(const std::wstring& rel) const {
        std::wstring p = path;
        size_t start = 0;
        while (start <= rel.size()) {
            size_t end = rel.find(L'\\', start);
            if (end == std::wstring::npos) end = rel.size();
            p += L"\\" + rel.substr(start, end - start);
            CreateDirectoryW(p.c_str(), nullptr);
            start = end + 1;
        }
        return p;
    }
    void write(const std::wstring& rel, const std::string& text) const {
        FILE* f = nullptr;
        _wfopen_s(&f, (path + L"\\" + rel).c_str(), L"wb");
        REQUIRE(f != nullptr);
        fwrite(text.data(), 1, text.size(), f);
        fclose(f);
    }
};

// JSON-escapes a Windows path ("\" -> "\\").
std::string J(const std::wstring& p) {
    std::string out;
    for (char c : WideToUtf8(p)) {
        if (c == '\\') out += "\\\\";
        else out += c;
    }
    return out;
}

const char* kProfile = R"(# DLC calibration profile (synthetic)
meter:
  model: i1d3      # colorimeter
  correction:
    file: null
    made: 2026-01-10   # read as text, not a date
    max_age_days: 120
displays:
  - name: Panel A
    desktoplut_monitor: 0
    primary: true
    panel: { tech: miniled, backlight_zones: 1000 }
    quirks:
      hardware_id: ABC1234
      short_name: "PA-1"   # quoted
  # a comment between list items
  - name: Panel B
    desktoplut_monitor: 1
    quirks:
      warmup: 3
  - name: 'Panel C ''parked'''
    desktoplut_monitor: 9
paths:
  output: results
  argyll: third_party\argyll   # backslashes stay as written
targets:
  srgb: { gamma: 2.2, white: D65 }
)";

}  // namespace

// ---- YAML -------------------------------------------------------------------------------------------

TEST_CASE("dlc yaml: block mappings, sequences, comments, flow maps and quoted scalars") {
    const YamlNode root = ParseYaml(kProfile);
    REQUIRE(root.kind == YamlNode::Map);
    const YamlNode* meter = root.find("meter");
    REQUIRE(meter != nullptr);
    CHECK(meter->str("model") == "i1d3");
    const YamlNode* corr = meter->find("correction");
    REQUIRE(corr != nullptr);
    CHECK(corr->find("file")->isNullish());
    CHECK(corr->str("made") == "2026-01-10");
    CHECK(corr->integer("max_age_days", 0) == 120);

    const YamlNode* ds = root.find("displays");
    REQUIRE(ds != nullptr);
    REQUIRE(ds->kind == YamlNode::Seq);
    REQUIRE(ds->seq.size() == 3);
    CHECK(ds->seq[0].str("name") == "Panel A");
    CHECK(ds->seq[0].integer("desktoplut_monitor", -1) == 0);
    const YamlNode* panel = ds->seq[0].find("panel");
    REQUIRE(panel != nullptr);
    CHECK(panel->kind == YamlNode::Map);
    CHECK(panel->str("backlight_zones") == "1000");
    CHECK(ds->seq[0].find("quirks")->str("hardware_id") == "ABC1234");
    CHECK(ds->seq[0].find("quirks")->str("short_name") == "PA-1");
    CHECK(ds->seq[1].find("quirks")->str("hardware_id").empty());
    CHECK(ds->seq[2].str("name") == "Panel C 'parked'");
    CHECK(ds->seq[2].integer("desktoplut_monitor", -1) == 9);

    const YamlNode* paths = root.find("paths");
    REQUIRE(paths != nullptr);
    CHECK(paths->str("output") == "results");
    CHECK(paths->str("argyll") == "third_party\\argyll");
    CHECK(root.find("targets")->find("srgb")->str("gamma") == "2.2");
}

TEST_CASE("dlc yaml: sequences under a key at the same indent, flow lists, block scalars") {
    const YamlNode root = ParseYaml("list:\n- a\n- b  # c\nflow: [1, 2, 'x, y']\ntext: |\n  line one\n  line two\nafter: z\n");
    REQUIRE(root.find("list") != nullptr);
    REQUIRE(root.find("list")->seq.size() == 2);
    CHECK(root.find("list")->seq[1].scalar == "b");
    const YamlNode* flow = root.find("flow");
    REQUIRE(flow->kind == YamlNode::Seq);
    REQUIRE(flow->seq.size() == 3);
    CHECK(flow->seq[2].scalar == "x, y");
    CHECK(root.str("text") == "line one\nline two");
    CHECK(root.str("after") == "z");
}

TEST_CASE("dlc yaml: never throws; odd lines are skipped, the rest is kept") {
    CHECK(ParseYaml("").kind == YamlNode::Null);
    CHECK(ParseYaml("# only a comment\n").kind == YamlNode::Null);
    const YamlNode root = ParseYaml("\xEF\xBB\xBF" "a: 1\nthis line is not yaml\n\tb: 2\nc: \"q # not a comment\"\n");
    CHECK(root.str("a") == "1");
    CHECK(root.str("c") == "q # not a comment");
    // Deep or broken flow collections stop instead of recursing without bound.
    std::string deep = "x: ";
    for (int i = 0; i < 200; i++) deep += "[";
    CHECK_NOTHROW(ParseYaml(deep));
    CHECK_NOTHROW(ParseYaml("x: { a: [1, 2\ny: {"));
}

// ---- JSON (files mode) ------------------------------------------------------------------------------

TEST_CASE("dlc json: files mode reads Python's NaN / Infinity as null and skips a BOM; the pipe stays strict") {
    using namespace ipc_json;
    const std::string text = "\xEF\xBB\xBF{\"a\": NaN, \"b\": Infinity, \"c\": -Infinity, \"d\": 1e999, \"e\": 2}";
    JsonParser files(text, JsonOptions::Files());
    const JsonValue v = files.parse();
    CHECK(v.find("a")->type == JsonValue::Null);
    CHECK(v.find("b")->type == JsonValue::Null);
    CHECK(v.find("c")->type == JsonValue::Null);
    CHECK(v.find("d")->type == JsonValue::Null);
    CHECK(v.getNum("e") == 2.0);
    JsonParser strict(std::string("{\"a\": NaN}"));
    CHECK_THROWS(strict.parse());
}

// ---- parsers ----------------------------------------------------------------------------------------

TEST_CASE("dlc: profile displays, output folder and meter correction") {
    const Profile p = ParseProfile(kProfile, L"X:\\dlc");
    CHECK(p.readable);
    REQUIRE(p.displays.size() == 3);
    CHECK(p.displays[0].hardwareId == "ABC1234");
    CHECK(p.displays[1].hardwareId.empty());
    CHECK(p.displays[2].monitor == 9);
    CHECK(p.outputDir == L"X:\\dlc\\results");
    CHECK(p.correctionMade == "2026-01-10");
    CHECK(p.correctionMaxAgeDays == 120);
    const Profile abs = ParseProfile("paths:\n  output: Y:\\out\n", L"X:\\dlc");
    CHECK(abs.outputDir == L"Y:\\out");
    CHECK(abs.correctionMaxAgeDays == 180);   // default
    CHECK_FALSE(ParseProfile("", L"X:\\dlc").readable);
}

TEST_CASE("dlc: stack registry records, incomplete ones dropped") {
    const auto s = ParseStackRegistry(R"({"schema": 1, "stacks": {
        "Panel A:SDR": {"display": "Panel A", "mode": "sdr", "monitor": 0, "run_id": "r1",
                        "applied_at": "2026-02-01T10:00:00",
                        "mhc": {"base_lut": "X:\\dlc\\runs\\r1\\generated\\mhc_base_sdr.cube"},
                        "cube": {"cube_path": "X:\\dlc\\runs\\r1\\generated\\final_sdr.cube", "run_id": "r1"}},
        "Panel A:HDR": {"display": "Panel A", "mode": "HDR", "mhc": {}, "cube": null, "hdr_peak": {"cap_nits": NaN}},
        "broken": {"mode": "SDR"}
    }})");
    REQUIRE(s.size() == 2);
    CHECK(s[0].mode == "SDR");
    CHECK(s[0].baseLut == L"X:\\dlc\\runs\\r1\\generated\\mhc_base_sdr.cube");
    CHECK(s[0].cubeRunId == "r1");
    CHECK(s[1].baseLut.empty());
    CHECK(s[1].cubePath.empty());
    CHECK(ParseStackRegistry("{not json").empty());
}

TEST_CASE("dlc: correction store, schema 2 per mode and schema 1 flat records") {
    const auto v2 = ParseCorrectionStore(R"({"schema": 2, "displays": {"Panel A": {
        "SDR": {"display": "Panel A", "mode": "SDR", "correction_made": "2026-03-01", "correction_file": "x.ccmx"},
        "HDR": {"correction_made": "2026-03-02"}}}})");
    REQUIRE(v2.size() == 2);
    CHECK(v2[1].display == "Panel A");
    CHECK(v2[1].mode == "HDR");
    CHECK(v2[1].made == "2026-03-02");
    const auto v1 = ParseCorrectionStore(R"({"displays": {
        "Panel B": {"display": "Panel B", "correction_file": "Panel_B_HDR-x.ccmx", "correction_made": "2026-01-01"},
        "Panel C": {"correction_file": "plain.ccmx"}}})");
    REQUIRE(v1.size() == 2);
    CHECK(v1[0].mode == "HDR");     // _HDR token in the file name
    CHECK(v1[1].mode == "SDR");     // no token: SDR, as DLC assumes
    CHECK(v1[1].display == "Panel C");
}

TEST_CASE("dlc: report fields, metric default, verify-only evidence; other shapes rejected") {
    Report r;
    REQUIRE(ParseReport(R"({"flow": "verify-only", "monitor": 1, "mode": "HDR", "content_mode": "SDR",
        "display": "Panel A", "target": "t", "date": "2026-04-02",
        "verification": {"avg_de2000": 0.8, "p95_de2000": 2.2, "max_de2000": 2.5, "white_de2000": 0.2,
                         "grayscale_avg_de2000": 0.4, "patch_count": 333, "within_quality": true,
                         "worst": [{"rgb": [0.5, 0.25, 0.0], "de2000": 2.5}]},
        "lut3d": null, "deliverables": {"cube": null},
        "verify_only": {"measured_stack": {"runtime_cube": "X:\\out\\A\\a.cube"},
                        "verify_source": {"identity": {"hardware_id": "ABC1234"}}}})", r));
    CHECK(r.flow == "verify-only");
    CHECK(r.contentMode == "SDR");
    CHECK(r.verification.metric == "CIEDE2000");   // absent: the oldest reports are CIEDE2000
    CHECK(r.verification.patchCount == 333);
    CHECK(r.verification.withinQuality);
    REQUIRE(r.worst.size() == 1);
    CHECK(r.worst[0].rgb[1] == doctest::Approx(0.25));
    CHECK(r.runtimeCube == L"X:\\out\\A\\a.cube");
    CHECK(r.hardwareId == "ABC1234");
    CHECK(std::string(MetricLabel("dE_ITP")) == "\xCE\x94" "E ITP");

    Report other;
    CHECK_FALSE(ParseReport(R"({"run_dir": "x", "monitor": 0, "metric": "x"})", other));   // stage-CLI report
    CHECK_FALSE(ParseReport("[1,2]", other));
}

TEST_CASE("dlc: patch metrics keep rows with a signal, null components read as NaN") {
    const auto p = ParsePatchMetrics(R"([
        {"rgb": [0.25, 0, 0], "measured_xyz": [2.1, null, 0.13], "target_xyz": [2.2, 1.1, 0.1], "de2000": 2.4,
         "grayscale": false, "gamut_clamped": true},
        {"rgb": null, "de2000": 1.0},
        {"rgb": [1, 1, 1], "measured_xyz": [95, 100, 108], "target_xyz": [95, 100, 108.9], "de2000": 0.3, "grayscale": true}])");
    REQUIRE(p.size() == 2);
    CHECK(std::isnan(p[0].measured[1]));
    CHECK(p[0].clamped);
    CHECK(p[1].grayscale);
    CHECK(p[1].de == doctest::Approx(0.3));
}

TEST_CASE("dlc: run_done events, run folder names, paths and dates") {
    const std::string tail = std::string("ut cut line\n") +
        R"({"event": "stage_done", "data": {}})" "\n" +
        R"({"event": "run_done", "data": {"results_dir": "X:\\out\\A_2026-01-01_SDR"}})" "\n" +
        R"({"event": "run_done", "data": {"results_dir": "X:\\out\\A_2026-01-02_SDR"}})" "\n";
    const auto dirs = RunDoneResultsDirs(tail);
    REQUIRE(dirs.size() == 2);
    CHECK(dirs[1] == L"X:\\out\\A_2026-01-02_SDR");

    CHECK(IsRunFolderName(L"20261001_201900_800602_sdr_panel_a"));
    CHECK(IsRunFolderName(L"20260618_214429_hdr_panel_a"));
    CHECK_FALSE(IsRunFolderName(L"_rehearsal_20260101"));
    CHECK_FALSE(IsRunFolderName(L"20261001_201900_800602_xyz_panel"));
    CHECK_FALSE(IsRunFolderName(L"probes"));

    CHECK(SamePath(L"X:/Dlc/Runs/a.cube", L"x:\\dlc\\runs\\A.CUBE"));
    CHECK_FALSE(SamePath(L"", L""));
    CHECK(RunIdFromPath(L"X:\\dlc\\runs\\20260101_000000_sdr_a\\generated\\final_sdr.cube") == "20260101_000000_sdr_a");
    CHECK(RunIdFromPath(L"X:\\dlc\\runs\\20260101_000000_sdr_a") == "20260101_000000_sdr_a");
    CHECK(RunIdFromPath(L"X:\\dlc\\results\\a.cube").empty());
    CHECK(DaysBetween("2026-01-01", "2026-03-01") == 59);
    CHECK(DaysBetween("2026-03-01T10:00:00", "2026-03-01") == 0);
    CHECK(DaysBetween("garbage", "2026-03-01") == -1);
    CHECK(HardwareIdOf(L"ABC1234-16843009", L"") == "ABC1234");
    CHECK(HardwareIdOf(L"", L"\\\\?\\DISPLAY#XYZ0001#5&1&UID1#{guid}") == "XYZ0001");
    CHECK(HardwareIdOf(L"", L"").empty());
}

// ---- join -------------------------------------------------------------------------------------------

namespace {

Data SampleData() {
    Data d;
    d.today = "2026-06-01";
    d.profile = ParseProfile(kProfile, L"X:\\dlc");
    StackRecord a;
    a.display = "Panel A"; a.mode = "SDR"; a.runId = "20260101_000000_sdr_panel_a";
    a.baseLut = L"X:\\dlc\\runs\\20260101_000000_sdr_panel_a\\generated\\mhc_base_sdr.cube";
    a.cubePath = L"X:\\dlc\\runs\\20260101_000000_sdr_panel_a\\generated\\final_sdr.cube";
    d.stacks.push_back(a);
    StackRecord b;   // Panel B has no hardware id in the profile: matched by what it runs
    b.display = "Panel B"; b.mode = "HDR"; b.runId = "20260201_000000_hdr_panel_b";
    b.baseLut = L"X:\\dlc\\runs\\20260201_000000_hdr_panel_b\\generated\\mhc_base_hdr.cube";
    d.stacks.push_back(b);

    Report newest;   // verify-only of Panel A's SDR cube, 10 days old
    newest.display = "Panel A"; newest.mode = "SDR"; newest.date = "2026-05-22"; newest.flow = "verify-only";
    newest.verification.present = true; newest.verification.metric = "CIEDE2000";
    newest.runtimeCube = L"X:\\DLC\\RUNS\\20260101_000000_sdr_panel_a\\generated\\final_sdr.cube";
    Report full;     // the full run that made it
    full.display = "Panel A"; full.mode = "SDR"; full.date = "2026-01-01"; full.flow = "full";
    full.verification.present = true;
    full.cubePath = a.cubePath;
    Report mhcOnly;  // Panel B's MHC-only run, found by its run folder
    mhcOnly.display = "Panel B"; mhcOnly.mode = "HDR"; mhcOnly.date = "2026-02-01"; mhcOnly.flow = "mhc-only";
    mhcOnly.verification.present = true; mhcOnly.verification.metric = "dE_ITP";
    mhcOnly.runDir = L"X:\\dlc\\runs\\20260201_000000_hdr_panel_b";
    d.reports = { newest, mhcOnly, full };
    d.corrections.push_back({ "Panel A", "SDR", "2026-01-15", L"x.ccmx" });
    return d;
}

}  // namespace

TEST_CASE("dlc join: hardware id first, twins only by index, stack evidence, report identity, else unmatched") {
    const Data d = SampleData();
    LiveDisplay a;
    a.hardwareId = "abc1234";   // case-insensitive
    CHECK(MatchDisplay(d, a).name == "Panel A");
    CHECK(MatchDisplay(d, a).by == MatchBy::HardwareId);

    LiveDisplay b;              // no hardware id in the profile, but it runs Panel B's recorded MHC
    b.hardwareId = "QQQ9999";
    b.mhcSource[1] = L"x:/dlc/runs/20260201_000000_hdr_panel_b/generated/mhc_base_hdr.cube";
    CHECK(MatchDisplay(d, b).name == "Panel B");
    CHECK(MatchDisplay(d, b).by == MatchBy::StackEvidence);

    LiveDisplay stranger;
    stranger.hardwareId = "ZZZ0000";
    stranger.lut[0] = L"X:\\elsewhere\\x.cube";
    CHECK(MatchDisplay(d, stranger).name.empty());
    CHECK_FALSE(MatchDisplay(d, stranger).ambiguous);

    Data twins = d;
    twins.profile.displays[1].hardwareId = "ABC1234";   // Panel B becomes Panel A's identical twin
    LiveDisplay t;
    t.hardwareId = "ABC1234";
    t.monitor = 1;
    CHECK(MatchDisplay(twins, t).name == "Panel B");     // desktoplut_monitor breaks the tie
    t.monitor = 5;
    CHECK(MatchDisplay(twins, t).name.empty());
    CHECK(MatchDisplay(twins, t).ambiguous);

    Data byReport;
    Report r;
    r.display = "Panel X"; r.mode = "SDR"; r.hardwareId = "RRR1111";
    byReport.reports.push_back(r);
    LiveDisplay x;
    x.hardwareId = "RRR1111";
    CHECK(MatchDisplay(byReport, x).name == "Panel X");
    CHECK(MatchDisplay(byReport, x).by == MatchBy::ReportIdentity);
}

TEST_CASE("dlc join: layers against the record, the report that verified what runs now, reminders") {
    const Data d = SampleData();
    LiveDisplay a;
    a.mhcSource[0] = d.stacks[0].baseLut;
    a.lut[0] = L"x:/dlc/runs/20260101_000000_sdr_panel_a/generated/final_sdr.cube";
    ModeRecord rec = RecordFor(d, "Panel A", 0, a, 90);
    CHECK(rec.stack == 0);
    CHECK(rec.mhc == LayerState::Matches);
    CHECK(rec.cube == LayerState::Matches);
    CHECK(rec.current == 0);            // the newest verification of that cube
    CHECK(rec.latest == 0);
    CHECK(rec.verifiedDaysAgo == 10);
    CHECK(rec.correctionMade == "2026-01-15");
    CHECK(rec.correctionDaysAgo == 137);
    CHECK(rec.correctionStale);         // max_age_days 120 in the profile
    CHECK(rec.reminderDue);

    LiveDisplay changed = a;            // another cube loaded since DLC applied
    changed.lut[0] = L"X:\\elsewhere\\hand_made.cube";
    rec = RecordFor(d, "Panel A", 0, changed, 90);
    CHECK(rec.cube == LayerState::Differs);
    CHECK(rec.current == -1);           // nothing verified that cube
    CHECK(rec.latest == 0);

    LiveDisplay b;                      // MHC only: the report of the run that installed it
    b.mhcSource[1] = d.stacks[1].baseLut;
    rec = RecordFor(d, "Panel B", 1, b, 90);
    CHECK(rec.mhc == LayerState::Matches);
    CHECK(rec.cube == LayerState::Off);
    CHECK(rec.current == 1);
    CHECK(rec.verifiedDaysAgo == 120);
    CHECK(rec.reminderDue);             // older than 90 days
    const ModeRecord off = RecordFor(d, "Panel B", 1, b, 0);     // verification reminder off
    CHECK(off.correctionMade.empty());                           // profile file: null -> no correction (DLC)
    CHECK_FALSE(off.reminderDue);
    Data withFile = d;
    withFile.profile.correctionFile = "panel_b.ccmx";            // a profile correction file: its date counts
    const ModeRecord prof = RecordFor(withFile, "Panel B", 1, b, 0);
    CHECK(prof.correctionMade == "2026-01-10");
    CHECK(prof.correctionStale);
    CHECK(prof.reminderDue);
    Data storeNoFile = d;                                        // a store record without a file: none
    storeNoFile.corrections.push_back({ "Panel B", "HDR", "2026-05-01", L"" });
    storeNoFile.profile.correctionFile = "panel_b.ccmx";
    CHECK(RecordFor(storeNoFile, "Panel B", 1, b, 0).correctionMade.empty());

    LiveDisplay noRecord;
    noRecord.lut[1] = L"X:\\x.cube";
    rec = RecordFor(d, "Panel A", 1, noRecord, 90);
    CHECK(rec.stack == -1);
    CHECK(rec.cube == LayerState::Unrecorded);
    CHECK(rec.mhc == LayerState::Off);
}

// ---- scan -------------------------------------------------------------------------------------------

TEST_CASE("dlc scan: a folder with profile, registry, reports and runs; reports linked to their runs") {
    TempDir t;
    const std::wstring root = t.path;
    t.write(L"calibration_profile.yaml", kProfile);
    const std::wstring runA = t.mkdir(L"runs\\20260101_120000_000001_sdr_panel_a");
    const std::wstring runB = t.mkdir(L"runs\\20260301_120000_000002_sdr_panel_a");
    t.mkdir(L"runs\\probes");
    t.mkdir(L"runs\\20260101_120000_000001_sdr_panel_a\\reports");
    const std::wstring resA = t.mkdir(L"results\\Panel_A_2026-01-01_SDR");
    const std::wstring resB = t.mkdir(L"results\\Panel_A_2026-03-01_SDR_verify-only_20260301_120000_000002");
    t.mkdir(L"results\\_scratch");
    t.write(L"stack_registry.json", std::string(R"({"stacks": {"Panel A:SDR": {"display": "Panel A", "mode": "SDR",
        "run_id": "20260101_120000_000001_sdr_panel_a", "mhc": {"base_lut": ")") + J(runA) +
        R"(\\generated\\mhc_base_sdr.cube"}, "cube": {"cube_path": ")" + J(runA) + R"(\\generated\\final_sdr.cube"}}}})");
    t.write(L"runs\\20260101_120000_000001_sdr_panel_a\\events.jsonl",
            std::string(R"({"event": "run_done", "data": {"results_dir": ")") + J(resA) + "\"}}\n");
    const std::string lut3d = R"("lut3d": {"cube_path": ")" + J(runA) + R"(\\generated\\final_sdr.cube"})";
    t.write(L"results\\Panel_A_2026-01-01_SDR\\report.json",
            R"({"flow": "full", "mode": "SDR", "display": "Panel A", "date": "2026-01-01", )" + lut3d +
            R"(, "verification": {"metric": "CIEDE2000", "avg_de2000": 0.3}})");
    t.write(L"results\\Panel_A_2026-03-01_SDR_verify-only_20260301_120000_000002\\report.json",
            R"({"flow": "verify-only", "mode": "SDR", "display": "Panel A", "date": "2026-03-01", "verification": {"avg_de2000": NaN}})");
    t.write(L"results\\_scratch\\report.json", R"({"mode": "SDR", "display": "Scratch"})");
    t.write(L"runs\\20260101_120000_000001_sdr_panel_a\\reports\\verification_iter00_patch_metrics.json", "[]");
    t.write(L"runs\\20260101_120000_000001_sdr_panel_a\\reports\\verification_iter02_patch_metrics.json",
            R"([{"rgb": [1, 1, 1], "de2000": 0.5, "grayscale": true}])");

    CHECK(LooksLikeDlcFolder(root));
    ScanCache cache;
    auto d = ScanFolder(root, cache, "2026-03-10");
    REQUIRE(static_cast<bool>(d));
    CHECK(d->folder == root);
    CHECK(d->profileFound);
    REQUIRE(d->stacks.size() == 1);
    REQUIRE(d->reports.size() == 2);                    // the _scratch folder is not read
    CHECK(d->reports[0].date == "2026-03-01");          // newest first
    CHECK(SamePath(d->reports[0].runDir, runB));        // by the verify-only folder stamp
    CHECK(SamePath(d->reports[1].runDir, runA));        // by run_done
    CHECK(std::isnan(d->reports[0].verification.avg));  // NaN read as absent
    CHECK(PatchMetricsPath(runA).find(L"iter02") != std::wstring::npos);
    CHECK(PatchMetricsPath(runB).empty());

    // A second scan reuses the parsed reports; a rewritten one is read again.
    CHECK(cache.reports.size() == 2);
    t.write(L"results\\Panel_A_2026-01-01_SDR\\report.json",
            R"({"flow": "full", "mode": "SDR", "display": "Panel A", "date": "2026-01-01", )" + lut3d +
            R"(, "verification": {"avg_de2000": 0.25, "padding": "xxxxxxxx"}})");
    d = ScanFolder(root, cache, "2026-03-10");
    REQUIRE(d->reports.size() == 2);
    CHECK(d->reports[1].verification.avg == doctest::Approx(0.25));

    LiveDisplay live;
    live.hardwareId = "ABC1234";
    live.mhcSource[0] = runA + L"\\generated\\mhc_base_sdr.cube";
    live.lut[0] = runA + L"\\generated\\final_sdr.cube";
    const DisplayMatch m = MatchDisplay(*d, live);
    CHECK(m.name == "Panel A");
    const ModeRecord rec = RecordFor(*d, m.name, 0, live, 90);
    CHECK(rec.mhc == LayerState::Matches);
    CHECK(rec.cube == LayerState::Matches);
    CHECK(rec.current == 1);                            // the full run's report verified that cube

    // No folder, or one that does not exist: an empty scan.
    CHECK(ScanFolder(L"", cache, "2026-03-10")->folder.empty());
    CHECK(ScanFolder(root + L"\\missing", cache, "2026-03-10")->folder.empty());
    CHECK(ResolveFolder(L"X:\\configured", {}) == L"X:\\configured");
}

// Where an unconfigured DesktopLUT finds DLC: a release unpacked beside DLC, a DLC folder inside the exe's folder, a
// source checkout (bin\Release), then the folder DLC was last found in. The configured folder is authoritative.
TEST_CASE("dlc folder: candidates in order, the first one holding DLC's files wins, the setting is authoritative") {
    // Candidates: lexical (no file touched), full paths, no duplicates, the last one used at the end.
    const auto c = FolderCandidates(L"C:\\Apps\\Repo\\bin\\Release", L"d:\\dlc", L"C:\\Users\\me");
    REQUIRE(c.size() == 8);
    CHECK(c[0] == L"C:\\Apps\\Repo\\bin\\Release\\DLC");
    CHECK(c[1] == L"C:\\Apps\\Repo\\bin\\Release");
    CHECK(c[2] == L"C:\\Apps\\Repo\\bin\\DLC");
    CHECK(c[3] == L"C:\\Apps\\Repo\\DLC");
    CHECK(c[4] == L"C:\\Apps\\Repo\\bin\\Release\\DesktopLUT\\DLC");   // a repository clone inside the exe's folder
    CHECK(c[5] == L"C:\\Apps\\Repo\\bin\\DesktopLUT\\DLC");            // ... or beside it
    CHECK(c[6] == L"C:\\Users\\me\\DesktopLUT\\DLC");                  // where the DLC tab's setup puts it
    CHECK(c[7] == L"d:\\dlc");
    CHECK(FolderCandidates(L"C:\\Apps\\Repo\\bin\\Release", L"C:\\apps\\repo\\DLC\\").size() == 6);   // already a candidate
    CHECK(FolderCandidates(L"", L"D:\\DLC") == std::vector<std::wstring>{ L"D:\\DLC" });
    CHECK(FolderCandidates(L"", L"D:\\DLC", L"C:\\Users\\me") ==
          std::vector<std::wstring>{ L"C:\\Users\\me\\DesktopLUT\\DLC", L"D:\\DLC" });
    CHECK(FolderCandidates(L"", L"").empty());

    TempDir t;
    const std::wstring exe = t.mkdir(L"Tools\\DesktopLUT");
    const std::wstring last = t.mkdir(L"Elsewhere\\DLC");
    t.write(L"Elsewhere\\DLC\\stack_registry.json", "{}");
    bool detected = true;
    // Nothing next to the exe: the folder last found.
    CHECK(SamePath(ResolveFolder(L"", FolderCandidates(exe, last), &detected), last));
    CHECK(detected);
    // Beside the exe's folder (a release unpacked next to DLC) comes first...
    const std::wstring beside = t.mkdir(L"Tools\\DLC");
    t.write(L"Tools\\DLC\\calibration_profile.yaml", "displays: []\n");
    CHECK(SamePath(ResolveFolder(L"", FolderCandidates(exe, last)), beside));
    // ...and a DLC folder inside the exe's own folder before that. A DLC not set up yet (its sources beside the example
    // profile) counts; an empty "DLC" folder does not.
    const std::wstring inside = t.mkdir(L"Tools\\DesktopLUT\\DLC");
    CHECK(SamePath(ResolveFolder(L"", FolderCandidates(exe, last)), beside));
    t.mkdir(L"Tools\\DesktopLUT\\DLC\\src\\dlc");
    t.write(L"Tools\\DesktopLUT\\DLC\\calibration_profile.example.yaml", "displays: []\n");
    CHECK(LooksLikeDlcFolder(inside));
    CHECK(SamePath(ResolveFolder(L"", FolderCandidates(exe, last)), inside));
    // A source checkout: <repo>\bin\Release\DesktopLUT.exe finds <repo>\DLC.
    const std::wstring repoExe = t.mkdir(L"Repo\\bin\\Release");
    const std::wstring repoDlc = t.mkdir(L"Repo\\DLC");
    t.write(L"Repo\\DLC\\stack_registry.json", "{}");
    CHECK(SamePath(ResolveFolder(L"", FolderCandidates(repoExe, L"")), repoDlc));
    // The setting wins, even over a folder found next to the exe and even when it does not exist (nothing found then).
    CHECK(ResolveFolder(L"X:\\Not\\There", FolderCandidates(exe, last), &detected) == L"X:\\Not\\There");
    CHECK_FALSE(detected);
    ScanCache cache;
    CHECK(ScanFolder(L"X:\\Not\\There", cache, "2026-10-20")->folder.empty());
    // Nothing anywhere: none.
    CHECK(ResolveFolder(L"", FolderCandidates(t.mkdir(L"Bare\\App"), L""), &detected).empty());
    CHECK_FALSE(detected);
    // A release unpacked anywhere finds the clone the setup made in the user's folder (a fresh clone: DLC's sources
    // beside the example profile)...
    const std::wstring home = t.mkdir(L"Home");
    t.mkdir(L"Home\\DesktopLUT\\DLC\\src\\dlc");
    t.write(L"Home\\DesktopLUT\\DLC\\calibration_profile.example.yaml", "displays: []\n");
    CHECK(SamePath(ResolveFolder(L"", FolderCandidates(t.mkdir(L"Downloads\\DesktopLUT_v3.0.0"), L"", home), &detected),
                   home + L"\\DesktopLUT\\DLC"));
    CHECK(detected);
    // ...and a clone beside the release's folder comes before it.
    const std::wstring zipExe = t.mkdir(L"Apps\\DesktopLUT_v3.0.0");
    t.mkdir(L"Apps\\DesktopLUT\\DLC\\src\\dlc");
    t.write(L"Apps\\DesktopLUT\\DLC\\calibration_profile.example.yaml", "displays: []\n");
    CHECK(SamePath(ResolveFolder(L"", FolderCandidates(zipExe, L"", home)), t.path + L"\\Apps\\DesktopLUT\\DLC"));
}

// ---- getting DLC from zero (the DLC tab's setup) ---------------------------------------------------------------
namespace {

// The repository root: up from this source file (tests\test_dlc.cpp), else up from the working directory.
std::wstring RepoRoot() {
    auto up = [](std::wstring p) {
        for (int i = 0; i < 6 && !p.empty(); i++) {
            const DWORD a = GetFileAttributesW((p + L"\\DLC\\pyproject.toml").c_str());
            if (a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY)) return p;
            const size_t cut = p.find_last_of(L"\\/");
            if (cut == std::wstring::npos) break;
            p.resize(cut);
        }
        return std::wstring();
    };
    std::wstring here = Utf8ToWide(__FILE__);
    if (const size_t cut = here.find_last_of(L"\\/"); cut != std::wstring::npos) here.resize(cut);
    if (std::wstring r = up(here); !r.empty()) return r;
    wchar_t cwd[MAX_PATH] = {};
    GetCurrentDirectoryW(MAX_PATH, cwd);
    return up(cwd);
}

std::string ReadText(const std::wstring& path) {
    std::string out;
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    if (h == INVALID_HANDLE_VALUE) return out;
    char buf[65536];
    DWORD got = 0;
    while (ReadFile(h, buf, sizeof(buf), &got, nullptr) && got) out.append(buf, got);
    CloseHandle(h);
    return out;
}

std::wstring Native(const std::string& rel) {
    std::wstring w = Utf8ToWide(rel);
    for (wchar_t& c : w) if (c == L'/') c = L'\\';
    return w;
}

}  // namespace

// The setup request names real repository files and the commands DLC's own docs give: a renamed doc or a changed
// command breaks this test instead of a user's setup.
TEST_CASE("dlc setup: the setup request and the guide link name what the repository holds") {
    const std::wstring root = RepoRoot();
    REQUIRE_MESSAGE(!root.empty(), "the repository root (DLC\\pyproject.toml) was not found");
    const std::string prompt = SetupPrompt(L"C:\\Users\\me\\DesktopLUT", "3.0.0");
    const SetupFacts& f = SetupPromptFacts();
    REQUIRE(!f.paths.empty());
    for (const std::string& p : f.paths) {
        INFO(p);
        CHECK(prompt.find(p) != std::string::npos);
        const DWORD a = GetFileAttributesW((root + L"\\" + Native(p)).c_str());
        CHECK((a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY)));
    }
    // Every command / tool path appears in the tracked setup docs as written.
    std::string docs;
    for (const char* doc : { "DLC/README.md", "DLC/docs/operator-guide.md", "DLC/third_party/README.md" })
        docs += ReadText(root + L"\\" + Native(doc)) + "\n";
    REQUIRE(docs.size() > 1000);
    for (const std::string& c : f.docText) {
        INFO(c);
        CHECK(prompt.find(c) != std::string::npos);
        CHECK(docs.find(c) != std::string::npos);
    }
    // The Python floor matches the package's.
    CHECK(ReadText(root + L"\\DLC\\pyproject.toml").find("requires-python = \">=3.11\"") != std::string::npos);
    // The prompt: the repository, the folder DesktopLUT looks in, this version's tag, no run before the user asks.
    CHECK(prompt.find(std::string("git clone ") + kRepoGitUrl + " \"C:\\Users\\me\\DesktopLUT\"") != std::string::npos);
    CHECK(prompt.find("git checkout v3.0.0") != std::string::npos);
    CHECK(prompt.find("Don't start a calibration run until I ask") != std::string::npos);
    CHECK(SetupRepoFolder(L"C:\\Users\\me") == L"C:\\Users\\me\\DesktopLUT");
    CHECK(SetupRepoFolder(L"").empty());
    // The folder the prompt names is one DesktopLUT finds by itself.
    const auto c = FolderCandidates(L"D:\\Anywhere", L"", L"C:\\Users\\me");
    CHECK(std::find(c.begin(), c.end(), SetupRepoFolder(L"C:\\Users\\me") + L"\\DLC") != c.end());
    // The guide link: the repository's README for DLC, at a heading it has.
    const std::string url = kGuideUrl;
    CHECK(url.rfind(std::string(kRepoUrl) + "/blob/main/" + kGuideDoc + "#", 0) == 0);
    const std::string readme = ReadText(root + L"\\" + Native(kGuideDoc));
    const size_t at = readme.find(std::string("\n") + kGuideHeading);
    REQUIRE(at != std::string::npos);
    const size_t after = at + 1 + std::strlen(kGuideHeading);
    CHECK((after < readme.size() && (readme[after] == '\n' || readme[after] == '\r')));   // the whole heading
    std::string anchor = std::string(kGuideHeading).substr(3);   // GitHub: lower case, spaces to hyphens
    for (char& ch : anchor) ch = ch == ' ' ? '-' : (char)std::tolower((unsigned char)ch);
    CHECK(url.substr(url.find('#') + 1) == anchor);
}

// The DLC tab's other requests: one place (dlc_setup), the characterize flow's command line as the operator guide
// gives it, and a flow DLC has.
TEST_CASE("dlc setup: the run and characterize requests name the display, the mode and DLC's own flow") {
    const std::wstring root = RepoRoot();
    REQUIRE_MESSAGE(!root.empty(), "the repository root (DLC\\pyproject.toml) was not found");
    CHECK(CalibratePrompt("Studio 32", true) == "Calibrate Studio 32 (HDR) with DLC.");
    CHECK(OnboardCalibratePrompt("ABC 3201", false).find("\"ABC 3201\" (SDR)") != std::string::npos);
    CHECK(OnboardCalibratePrompt("ABC 3201", false).find("onboard it first") != std::string::npos);
    const std::string sdr = CharacterizePrompt("Studio 32", false), hdr = CharacterizePrompt("Studio 32", true);
    CHECK(sdr.rfind("Characterize Studio 32 (SDR) with DLC", 0) == 0);
    CHECK(hdr.find(std::string(kCharacterizeFlow) + " HDR") != std::string::npos);
    CHECK(sdr.find("No calibration run after it unless I ask") != std::string::npos);
    const std::string onboard = OnboardCharacterizePrompt("ABC 3201", true);
    CHECK(onboard.find("\"ABC 3201\" (HDR)") != std::string::npos);
    CHECK(onboard.find("onboard it first") != std::string::npos);
    CHECK(onboard.find(std::string(kCharacterizeFlow) + " HDR") != std::string::npos);
    // The operator guide writes the command as the prompt does, and DLC's orchestrator has the flow.
    const std::string guide = ReadText(root + L"\\" + Native(kCharacterizeDoc));
    REQUIRE(guide.size() > 1000);
    CHECK(guide.find(std::string(kCharacterizeFlow) + " SDR") != std::string::npos);
    CHECK(sdr.find(std::string(kCharacterizeFlow) + " SDR") != std::string::npos);
    CHECK(ReadText(root + L"\\DLC\\src\\dlc\\calibrate.py").find("\"characterize\": (") != std::string::npos);
}

namespace {
const DipRecord* RecordOf(const DipStoreData& d, const std::string& key) {
    for (const DipRecord& r : d.records) if (r.key == key) return &r;
    return nullptr;
}
}  // namespace

// tests/fixtures/dip_store_panel.json: a full HDR record (with sections DLC does not have yet and fields a newer DLC
// might write), a record with only its display and mode, a numeric string, three records DLC's loader drops (a
// field that is no number, a record that is no object, a noise band without sigma_de), a mode-less one with NaN.
TEST_CASE("dlc dip: every record summarised as DLC loads it; the malformed ones dropped, the rest kept") {
    const std::wstring root = RepoRoot();
    REQUIRE(!root.empty());
    const std::string json = ReadText(root + L"\\tests\\fixtures\\dip_store_panel.json");
    REQUIRE(!json.empty());
    const DipStoreData d = ParseDipStore(json);
    REQUIRE(d.readable);
    CHECK(d.records.size() == 4);
    REQUIRE(d.dropped.size() == 3);   // dip.py DipStore.load drops the same three (checked against it, 2026-11-12)
    CHECK(d.dropped[0] == "Bright 24:SDR");
    CHECK(d.dropped[1] == "Flat 22:HDR");
    CHECK(d.dropped[2] == "Banded 26:SDR");

    const DipRecord* full = RecordOf(d, "Studio 32:HDR");
    REQUIRE(full);
    CHECK(full->display == "Studio 32");
    CHECK(full->mode == "HDR");
    CHECK(full->made == "2026-06-19");
    CHECK(full->updated == "2026-10-23");
    CHECK(full->instrument == "Example Colorimeter Pro");
    CHECK(full->correctionFile.empty());
    CHECK(full->noiseBands == 2);
    CHECK(full->noiseFloorNits == 0.0);
    CHECK(full->settleS == doctest::Approx(1.66));
    CHECK(full->settleBrightS == doctest::Approx(1.66));
    CHECK(full->settleDarkS == 0.0);
    CHECK(full->whiteNits == doctest::Approx(1835.1));
    CHECK(full->whiteX == doctest::Approx(0.3169));
    CHECK(full->whiteY == doctest::Approx(0.3284));
    CHECK(full->blackNits == 0.0);
    CHECK(full->hasPrimaries);
    CHECK(std::isnan(full->sustainedPeakNits));   // declared by DLC, null
    CHECK(full->eotfUndershoot == doctest::Approx(0.0145));
    CHECK(full->whiteVsLuminance == 2);
    CHECK(full->thermalRegime == "convergent");
    CHECK(full->warmupMinutes == doctest::Approx(29.57));
    CHECK(full->coldChannel == "R");
    CHECK(full->maxAgeDays == -1);
    CHECK(std::isnan(DipContrast(*full)));        // black 0: below the meter, no ratio
    REQUIRE(full->refresh >= 0);
    REQUIRE(full->refresh < (int)d.refresh.size());
    const DipRefreshSection& rr = d.refresh[(size_t)full->refresh];
    CHECK(rr.recordKey == "Studio 32:HDR");
    CHECK(rr.measured == "2026-10-23");
    CHECK(rr.error.empty());
    CHECK(rr.rates.size() == 2);
    CHECK(rr.hardwareId == "ABC3201");
    CHECK(full->peakByWindow.present);
    CHECK(full->peakByWindow.measured == "2026-11-10");
    CHECK(DipAgeDays(*full, "2026-11-12") == 146);
    CHECK_FALSE(DipStale(*full, "2026-11-12"));
    CHECK_FALSE(DipStale(*full, "2026-12-16"));   // 180 days: DLC's default limit, not past it
    CHECK(DipStale(*full, "2026-12-17"));

    // Only the mandatory fields: every number unmeasured, no sections, no date (DLC calls that stale).
    const DipRecord* min = RecordOf(d, "Studio 32:SDR");
    REQUIRE(min);
    CHECK(min->mode == "SDR");
    CHECK(min->noiseBands == 0);
    for (double v : { min->noiseFloorNits, min->readOverheadS, min->settleS, min->whiteNits, min->whiteX, min->whiteY,
                      min->blackNits, min->sustainedPeakNits, min->eotfUndershoot, min->warmupMinutes })
        CHECK(std::isnan(v));
    CHECK(min->thermalRegime.empty());
    CHECK(min->instrument.empty());
    CHECK(min->refresh == -1);
    CHECK_FALSE(min->peakByWindow.present);
    CHECK_FALSE(min->hasPrimaries);
    CHECK(DipAgeDays(*min, "2026-11-12") == -1);
    CHECK(DipStale(*min, "2026-11-12"));

    // A number written as a string (Python's float() takes it); the record's own age limit.
    const DipRecord* office = RecordOf(d, "Office 27:SDR");
    REQUIRE(office);
    CHECK(office->noiseFloorNits == doctest::Approx(0.05));
    CHECK(DipContrast(*office) == doctest::Approx(2200.0));
    CHECK(office->maxAgeDays == 30);
    CHECK(office->correctionFile == "corrections\\office27.ccmx");
    CHECK_FALSE(DipStale(*office, "2026-10-01"));
    CHECK(DipStale(*office, "2026-10-02"));

    // Mode-less (written before mode-keying); a NaN DLC wrote reads as unmeasured.
    const DipRecord* legacy = RecordOf(d, "Legacy 21");
    REQUIRE(legacy);
    CHECK(legacy->mode.empty());
    CHECK(legacy->display == "Legacy 21");
    CHECK(std::isnan(legacy->blackNits));

    // No file, not a store: no records.
    CHECK(ParseDipStore("").records.empty());
    CHECK_FALSE(ParseDipStore("[]").readable);
    CHECK(ParseDipStore(R"({"schema": 1})").records.empty());
    DipStoreData none;
    uint64_t hash = 0;
    TempDir empty;
    CHECK_FALSE(ReadDipStore(empty.path, none, hash));
    CHECK(none.records.empty());
    CHECK(hash == 0);
}

// The PANEL card's one number format: significant digits per quantity, never a "0" for what is not zero.
TEST_CASE("dlc: FormatSig keeps every measured digit that matters and never prints 0 for a non-zero value") {
    CHECK(FormatSig(1835.101779, 3) == "1835");     // whole digits are never rounded away
    CHECK(FormatSig(121.722159, 3) == "122");
    CHECK(FormatSig(114.515401, 3) == "115");
    CHECK(FormatSig(0.0021, 2) == "0.0021");
    CHECK(FormatSig(0.0021, 3) == "0.0021");        // trailing zeros dropped, not padded
    CHECK(FormatSig(0.05376, 3) == "0.0538");
    CHECK(FormatSig(0.05376, 2) == "0.054");
    CHECK(FormatSig(1.6597, 2) == "1.7");
    CHECK(FormatSig(29.5697, 2) == "30");
    CHECK(FormatSig(3.469, 2) == "3.5");
    CHECK(FormatSig(0.00999, 2) == "0.01");         // rounding into the next decade
    CHECK(FormatSig(1.453, 2, true) == "+1.5");
    CHECK(FormatSig(-1.217, 2, true) == "-1.2");
    CHECK(FormatSig(0.0004, 2, true) == "+0.0004");
    CHECK(FormatSig(-0.00004, 1) == "-0.00004");
    CHECK(FormatSig(2130.1, 3) == "2130");
    CHECK(FormatSig(0.3169243, 4) == "0.3169");
    CHECK(FormatSig(0.0, 3) == "0");                // exactly zero is zero
    CHECK(FormatSig(0.0, 3, true) == "0");
    CHECK(FormatSig(kNaN, 3).empty());
    // Below every fixed decimal: still not "0".
    CHECK(FormatSig(3e-17, 2) != "0");
    CHECK(FormatSig(3e-17, 2).find('e') != std::string::npos);
    for (double v : { 1e-9, 4.9e-7, 0.00049, 0.0049, 0.049, 0.49 }) {
        INFO(v);
        CHECK(FormatSig(v, 1) != "0");
        CHECK(std::strtod(FormatSig(v, 2).c_str(), nullptr) > 0.0);
    }
}

namespace {
std::string FixtureText(const wchar_t* name) {
    const std::wstring root = RepoRoot();
    return root.empty() ? std::string() : ReadText(root + L"\\tests\\fixtures\\" + name);
}
const DipRecord* RecordAt(const DipStoreData& d, const std::string& key) {
    for (const DipRecord& r : d.records) if (r.key == key) return &r;
    return nullptr;
}
const DipPeakSize* SizeAt(const DipPeakByWindow& p, double area) {
    for (const DipPeakSize& z : p.curve) if (std::fabs(z.areaPct - area) < 1e-9) return &z;
    return nullptr;
}
}  // namespace

// tests/fixtures/dip_store_probes.json is written by DLC's own code (tests/fixtures/gen_dip_probes.py runs the
// peak-window and refresh-response flows on DLC's probe simulator and records them through dip.py): what DLC writes,
// not a copy of its format. `--check` there says when DLC's format moved.
TEST_CASE("dlc dip: peak_by_window as DLC records it — an OLED's steep curve, a mini-LED's flat one, a drift's invalid one") {
    const std::string json = FixtureText(L"dip_store_probes.json");
    REQUIRE(!json.empty());
    const DipStoreData d = ParseDipStore(json);
    REQUIRE(d.readable);
    CHECK(d.dropped.empty());

    const DipRecord* oled = RecordAt(d, "Probe OLED 42:HDR");
    REQUIRE(oled);
    const DipPeakByWindow& o = oled->peakByWindow;
    REQUIRE(o.Usable());
    CHECK(o.schema == kDipPeakWindowSchema);
    CHECK_FALSE(o.newer);
    CHECK(o.valid);
    CHECK(o.complete);
    CHECK(o.measured == "2026-11-12");
    CHECK(!o.runId.empty());
    REQUIRE(o.curve.size() == 9);
    CHECK(o.curve.front().areaPct == 1.0);
    CHECK(o.curve.back().areaPct == 100.0);
    CHECK(o.plateauNits == doctest::Approx(1000.0).epsilon(0.02));
    CHECK(o.runWindowAreaPct == doctest::Approx(10.0));
    CHECK(o.fullFieldSustainedNits < 0.35 * o.plateauNits);   // the power limiter
    const DipPeakSize* half = SizeAt(o, 50.0);
    REQUIRE(half);
    CHECK(half->agree == 1);
    CHECK(half->up.present);
    CHECK(half->down.present);
    CHECK(half->early > 1.05 * half->sustained);              // the limiter acts over the hold
    CHECK(std::isfinite(half->tau));
    CHECK(SizeAt(o, 100.0)->agree == -1);                     // the turn: measured once
    CHECK_FALSE(SizeAt(o, 100.0)->down.present);
    CHECK(o.stack == "native");
    CHECK(o.emissive == 1);
    CHECK(o.steps > 0);
    CHECK(std::isfinite(o.peakWhiteS));

    const DipRecord* mini = RecordAt(d, "Probe MiniLED 32:HDR");
    REQUIRE(mini);
    REQUIRE(mini->peakByWindow.Usable());
    CHECK(mini->peakByWindow.valid);
    CHECK(mini->peakByWindow.fullFieldSustainedNits > 0.6 * mini->peakByWindow.plateauNits);   // flat-ish
    CHECK(mini->peakByWindow.emissive == 0);

    // Drift: the passes disagree at most sizes, with one sign. No combined value there; each pass's own remains.
    const DipRecord* drift = RecordAt(d, "Probe Drift 27:HDR");
    REQUIRE(drift);
    const DipPeakByWindow& dr = drift->peakByWindow;
    REQUIRE(dr.Usable());
    CHECK_FALSE(dr.valid);
    CHECK(dr.systematic);
    REQUIRE(!dr.areasFailed.empty());
    for (double a : dr.areasFailed) {
        const DipPeakSize* z = SizeAt(dr, a);
        REQUIRE(z);
        CHECK(z->agree == 0);
        CHECK(std::isnan(z->sustained));
        CHECK(std::isnan(z->early));
        CHECK(std::isfinite(z->up.sustained));
        CHECK(std::isfinite(z->down.sustained));
        CHECK(z->relDiff > z->tol);
    }
}

TEST_CASE("dlc dip: a peak_by_window DLC would drop is dropped alone; a newer schema is said, not read") {
    // Unknown fields are ignored.
    DipPeakByWindow p = ParsePeakByWindowJson(R"({"measured": "2026-11-12", "a_future_field": [1, {"x": 2}],
        "curve": [{"area_pct": 1, "sustained_nits": 900, "early_nits": 950, "something_new": "yes"},
                  {"area_pct": 10, "sustained_nits": null}], "valid": true})");
    CHECK(p.Usable());
    CHECK(p.curve.size() == 2);
    CHECK(std::isnan(p.curve[1].sustained));
    // What dip.py PeakByWindow.from_dict refuses.
    const char* refused[] = {
        R"([1, 2])",
        R"({"measured": "2026-11-12", "curve": []})",
        R"({"measured": "2026-11-12"})",
        R"({"curve": [{"area_pct": 1}]})",
        R"({"measured": "2026-11-12", "curve": [{"area_pct": 250}]})",
        R"({"measured": "2026-11-12", "curve": [{"area_pct": 0}]})",
        R"({"measured": "2026-11-12", "curve": [{"area_pct": 5, "sustained_nits": -1}]})",
        R"({"measured": "2026-11-12", "curve": [{"area_pct": 5, "tau_s": "slow"}]})",
        R"({"measured": "2026-11-12", "curve": [7]})",
        R"({"measured": "2026-11-12", "curve": [{"area_pct": 5}], "steps": [{"area_pct": 120}]})",
        R"({"measured": "2026-11-12", "curve": [{"area_pct": 5}], "plateau_nits": -3})",
        R"({"measured": "2026-11-12", "curve": [{"area_pct": 5}], "wear": [1]})",
        R"({"measured": "2026-11-12", "curve": [{"area_pct": 5}], "schema": null})",
    };
    for (const char* j : refused) {
        CAPTURE(j);
        p = ParsePeakByWindowJson(j);
        CHECK(p.present);
        CHECK_FALSE(p.error.empty());
        CHECK_FALSE(p.Usable());
        CHECK(p.curve.empty());
    }
    p = ParsePeakByWindowJson(R"({"schema": 2, "measured": "2026-11-12", "curve": [{"area_pct": 1}]})");
    CHECK(p.newer);
    CHECK(p.error.empty());
    CHECK(p.schema == 2);
    CHECK(p.curve.empty());
    CHECK_FALSE(p.Usable());
    // Inside a store: the record stays, with its other numbers, and says the section is unreadable.
    const DipStoreData d = ParseDipStore(R"({"displays": {"X:HDR": {"display": "X", "mode": "HDR", "made": "2026-11-01",
        "native_white_nits": 640, "peak_by_window": {"measured": "2026-11-12", "curve": [{"area_pct": 250}]}}}})");
    REQUIRE(d.records.size() == 1);
    CHECK(d.dropped.empty());
    CHECK(d.records[0].whiteNits == 640.0);
    CHECK(d.records[0].peakByWindow.present);
    CHECK_FALSE(d.records[0].peakByWindow.error.empty());
}

TEST_CASE("dlc dip: the refresh response's reference, its VRR-derived rates and the VRR sweep, as DLC records them") {
    const DipStoreData d = ParseDipStore(FixtureText(L"dip_store_probes.json"));
    REQUIRE(d.readable);
    const DipRecord* rec = RecordAt(d, "Probe VRR 27:HDR");
    REQUIRE(rec);
    REQUIRE(rec->refresh >= 0);
    const DipRefreshSection& s = d.refresh[(size_t)rec->refresh];
    CHECK(s.error.empty());
    CHECK(s.referenceRecorded);
    CHECK(s.referenceKey == "60");
    CHECK(s.ReferenceHz() == 60.0);
    CHECK(s.hardwareId == "PRB0042");
    std::map<std::string, const DipRefreshRate*> by;
    for (const DipRefreshRate& r : s.rates) by[r.key] = &r;
    REQUIRE(by.count("47.952"));
    CHECK(by["47.952"]->source == "mode");
    CHECK(by["47.952"]->proofOk == 1);
    for (const char* k : { "48", "50", "59.94" }) {
        CAPTURE(k);
        REQUIRE(by.count(k));
        CHECK(by[k]->source == "vrr");
        CHECK(by[k]->proofOk == 1);
        CHECK(by[k]->proofKind.rfind("vrr:", 0) == 0);
    }
    CHECK(s.vrr.present);
    CHECK(s.vrr.rangeLoHz == 40.0);
    CHECK(s.vrr.rangeHiHz == 60.0);
    CHECK(s.vrr.points > 0);
    CHECK(s.vrr.crossCheckOk == 1);
    CHECK(s.vrr.crossCheckRate == "47.952");
    CHECK(s.vrr.fixedModeTrimDerived);

    // The reference block: recorded as written (another rate), required to name a positive rational, absent = not
    // recorded (dip.py's 60 / 1 kept as the fields' value, but not trusted by the trim).
    auto section = [](const char* ref) {
        const std::string j = std::string(R"({"displays": {"X:HDR": {"display": "X", "refresh_response": {"measured": "2026-10-23", )") +
                              ref + R"("rates": [{"key": "48", "num": 48, "den": 1, "levels": [{"nits": 1, "ratio": 1.02}]}]}}}})";
        const DipStoreData dd = ParseDipStore(j);
        REQUIRE(dd.refresh.size() == 1);
        return dd.refresh[0];
    };
    const DipRefreshSection r120 = section(R"("reference": {"key": "120", "num": 120, "den": 1}, )");
    CHECK(r120.error.empty());
    CHECK(r120.referenceRecorded);
    CHECK(r120.ReferenceHz() == 120.0);
    CHECK(r120.referenceKey == "120");
    const DipRefreshSection r5994 = section(R"("reference": {"num": 60000, "den": 1001}, )");
    CHECK(r5994.referenceKey == "59.94");          // no key: the catalogue's spelling of its rational
    for (const char* bad : { R"("reference": {"key": "120"}, )", R"("reference": {"key": "120", "num": 0, "den": 1}, )",
                             R"("reference": "60", )" }) {
        CAPTURE(bad);
        const DipRefreshSection b = section(bad);
        CHECK_FALSE(b.error.empty());
        CHECK(b.rates.empty());
    }
    const DipRefreshSection none = section("");
    CHECK(none.error.empty());
    CHECK_FALSE(none.referenceRecorded);
    CHECK(none.referenceKey == "60");
}

// The live payloads (charts.json "probes"), from the same generator: DLC's DashboardState after the flows ran.
TEST_CASE("dlc live: a probe flow's chart payloads — peak by window in the DIP section's shape, the refresh points by source") {
    const std::string json = FixtureText(L"charts_probes.json");
    REQUIRE(!json.empty());
    LiveCharts c;
    REQUIRE(ParseLiveCharts(json, c));
    const DipPeakByWindow& p = c.probes.peakByWindow;
    REQUIRE(p.present);
    CHECK(p.complete);
    CHECK(p.valid);
    REQUIRE(p.curve.size() == 9);
    CHECK(p.runWindowAreaPct == doctest::Approx(10.0));
    CHECK(p.plateauNits == doctest::Approx(1000.0).epsilon(0.02));
    CHECK(p.stepsDone == p.stepsPlanned);
    const DipPeakSize* half = SizeAt(p, 50.0);
    REQUIRE(half);
    CHECK(half->agree == 1);
    CHECK(half->up.present);
    CHECK(half->down.present);
    CHECK(std::isfinite(half->sustained));
    CHECK(half->early > half->sustained);
    CHECK(SizeAt(p, 100.0)->agree == -1);

    const RefreshProbeChart& r = c.probes.refresh;
    REQUIRE(r.present);
    CHECK(r.referenceHz == 60.0);
    REQUIRE(!r.series.empty());
    int vrr = 0, mode = 0;
    for (const RefreshProbeSeries& s : r.series)
        for (const RefreshProbePoint& q : s.points) (q.vrr ? vrr : mode)++;
    CHECK(vrr > 0);
    CHECK(mode > 0);

    // The drifting panel's payload: the passes disagree, so the curve has no combined value there.
    ipc_json::JsonValue root;
    {
        ipc_json::JsonParser parser(json, ipc_json::JsonOptions::Files());
        root = parser.parse();
    }
    const ipc_json::JsonValue* byPanel = root.find("probes_by_panel");
    REQUIRE(byPanel);
    const ipc_json::JsonValue* drift = byPanel->find("Probe Drift 27");
    REQUIRE(drift);
    std::string one = "{\"probes\": {\"peak_by_window\": ";
    ipc_json::Serialize(*drift, one);
    one += "}}";
    const ProbeCharts dc = ParseProbeCharts(one);
    REQUIRE(dc.peakByWindow.present);
    CHECK(dc.peakByWindow.complete);
    CHECK_FALSE(dc.peakByWindow.valid);
    CHECK_FALSE(dc.peakByWindow.areasFailed.empty());
    for (double a : dc.peakByWindow.areasFailed) CHECK(SizeAt(dc.peakByWindow, a)->agree == 0);
    // A run still going: no verdict yet, nothing judged.
    const ProbeCharts running = ParseProbeCharts(R"({"probes": {"peak_by_window": {"complete": false, "steps_done": 3,
        "steps_planned": 17, "series": [{"name": "early", "pass": "up", "points": [[1, 990], [2, 985]]},
                                        {"name": "sustained", "pass": "up", "points": [[1, 980], [2, 970], [3, 960]]}]}}})");
    REQUIRE(running.peakByWindow.present);
    CHECK_FALSE(running.peakByWindow.complete);
    CHECK(running.peakByWindow.curve.size() == 3);
    for (const DipPeakSize& z : running.peakByWindow.curve) CHECK(z.agree == -1);
    CHECK(running.peakByWindow.stepsDone == 3);
    CHECK_FALSE(ParseProbeCharts("{}").peakByWindow.present);
    CHECK_FALSE(ParseProbeCharts("{}").refresh.present);
}

TEST_CASE("dlc dip: a display's record as DLC looks it up; the hardware id breaks ties for the refresh response") {
    const std::wstring root = RepoRoot();
    REQUIRE(!root.empty());
    Data data;
    data.dip = ParseDipStore(ReadText(root + L"\\tests\\fixtures\\dip_store_panel.json"));
    data.dipFound = data.dip.readable;
    REQUIRE(data.dip.records.size() == 4);
    LiveDisplay live;
    live.hardwareId = "ABC3201";
    auto keyOf = [&data](const DipLink& l) { return l.record >= 0 ? data.dip.records[(size_t)l.record].key : std::string(); };

    DipLink hdr = DipRecordFor(data, "Studio 32", 1, live);
    CHECK(keyOf(hdr) == "Studio 32:HDR");
    CHECK(hdr.key == "Studio 32:HDR");
    CHECK_FALSE(hdr.modeless);
    CHECK_FALSE(hdr.refreshOtherPanel);
    CHECK(keyOf(DipRecordFor(data, "Studio 32", 0, live)) == "Studio 32:SDR");
    // Keys are exact, as DLC's dict is.
    CHECK(DipRecordFor(data, "studio 32", 1, live).record == -1);
    // The mode-less record serves either mode.
    const DipLink legacy = DipRecordFor(data, "Legacy 21", 1, live);
    CHECK(keyOf(legacy) == "Legacy 21");
    CHECK(legacy.modeless);
    // A record DLC drops: none, said so.
    const DipLink bright = DipRecordFor(data, "Bright 24", 0, live);
    CHECK(bright.record == -1);
    CHECK(bright.unreadable);
    CHECK(DipRecordFor(data, "Flat 22", 1, live).unreadable);
    // No record for that mode / no such display / not matched.
    const DipLink officeHdr = DipRecordFor(data, "Office 27", 1, live);
    CHECK(officeHdr.record == -1);
    CHECK_FALSE(officeHdr.unreadable);
    CHECK(DipRecordFor(data, "Nobody", 0, live).record == -1);
    CHECK(DipRecordFor(data, "", 1, live).record == -1);
    // The refresh response names another panel: the record stays, its response is not this display's.
    live.hardwareId = "XYZ9999";
    hdr = DipRecordFor(data, "Studio 32", 1, live);
    CHECK(keyOf(hdr) == "Studio 32:HDR");
    CHECK(hdr.refreshOtherPanel);
    live.hardwareId.clear();   // unknown: no verdict
    CHECK_FALSE(DipRecordFor(data, "Studio 32", 1, live).refreshOtherPanel);
}

// Developer check against a real DLC folder (read-only): set DLUT_REAL_DLC to its path. Skipped otherwise.
TEST_CASE("dlc scan: a real DLC folder (DLUT_REAL_DLC)" * doctest::skip(GetEnvironmentVariableW(L"DLUT_REAL_DLC", nullptr, 0) == 0)) {
    wchar_t folder[MAX_PATH] = {};
    GetEnvironmentVariableW(L"DLUT_REAL_DLC", folder, MAX_PATH);
    ScanCache cache;
    auto d = ScanFolder(folder, cache, "2026-10-10");
    MESSAGE("profile " << d->profileFound << " displays " << d->profile.displays.size() << " stacks " << d->stacks.size()
            << " corrections " << d->corrections.size() << " reports " << d->reports.size());
    int linked = 0;
    for (const Report& r : d->reports) {
        if (!r.runDir.empty()) linked++;
        MESSAGE(r.date << " " << r.display << " " << r.mode << " " << r.flow << " " << r.verification.metric << " avg "
                << r.verification.avg << " run " << (r.runDir.empty() ? std::string("-") : RunIdFromPath(r.runDir))
                << (PatchMetricsPath(r.runDir).empty() ? "" : " +patches"));
    }
    MESSAGE("reports linked to a run: " << linked << " / " << d->reports.size());
    for (const StackRecord& s : d->stacks)
        MESSAGE("stack " << s.display << ":" << s.mode << " run " << s.runId << " mhc " << (s.baseLut.empty() ? "-" : "yes")
                << " cube " << (s.cubePath.empty() ? "-" : "yes"));
    for (const DipRecord& r : d->dip.records)
        MESSAGE("dip " << r.key << " made " << r.made << " white " << r.whiteNits << " black " << r.blackNits << " refresh "
                << std::string(r.refresh >= 0 ? "yes" : "-") << " peak_by_window "
                << std::string(r.peakByWindow.present ? "yes" : "-"));
    MESSAGE("dip records dropped: " << d->dip.dropped.size());
    CHECK(d->folder == folder);
}

// Developer check: the join for each [Display<n>] of a real DesktopLUT.ini (read-only), against the real DLC
// folder. Set DLUT_REAL_DLC and DLUT_REAL_INI. Skipped otherwise.
TEST_CASE("dlc join: a real INI against a real DLC folder (DLUT_REAL_INI)" *
          doctest::skip(GetEnvironmentVariableW(L"DLUT_REAL_INI", nullptr, 0) == 0 ||
                        GetEnvironmentVariableW(L"DLUT_REAL_DLC", nullptr, 0) == 0)) {
    wchar_t folder[MAX_PATH] = {}, ini[MAX_PATH] = {};
    GetEnvironmentVariableW(L"DLUT_REAL_DLC", folder, MAX_PATH);
    GetEnvironmentVariableW(L"DLUT_REAL_INI", ini, MAX_PATH);
    ScanCache cache;
    auto d = ScanFolder(folder, cache, "2026-10-11");
    auto get = [&](const std::wstring& sec, const wchar_t* key) {
        wchar_t buf[2048] = {};
        GetPrivateProfileStringW(sec.c_str(), key, L"", buf, 2048, ini);
        return std::wstring(buf);
    };
    for (int n = 0; n < 10; n++) {
        const std::wstring sec = L"Display" + std::to_wstring(n);
        const std::wstring edid = get(sec, L"EdidId");
        if (edid.empty()) continue;
        LiveDisplay live;
        live.hardwareId = HardwareIdOf(edid, get(sec, L"DevicePath"));
        live.monitor = n;
        live.lut[0] = get(sec, L"LUT_SDR");
        live.lut[1] = get(sec, L"LUT_HDR");
        if (get(sec, L"SDR_MHCEnabled") == L"true") live.mhcSource[0] = get(sec, L"SDR_MHCSourceFile");
        if (get(sec, L"HDR_MHCEnabled") == L"true") live.mhcSource[1] = get(sec, L"HDR_MHCSourceFile");
        const DisplayMatch m = MatchDisplay(*d, live);
        MESSAGE(WideToUtf8(sec) << " " << live.hardwareId << " -> " << (m.name.empty() ? "(none)" : m.name) << " by " << (int)m.by
                << (m.ambiguous ? " AMBIGUOUS" : ""));
        if (m.name.empty()) continue;
        for (int k = 0; k < 2; k++) {
            const ModeRecord rec = RecordFor(*d, m.name, k, live, 90);
            const Report* cur = rec.current >= 0 ? &d->reports[rec.current] : nullptr;
            const Report* lat = rec.latest >= 0 ? &d->reports[rec.latest] : nullptr;
            MESSAGE("  " << (k ? "HDR" : "SDR") << " mhc " << (int)rec.mhc << " cube " << (int)rec.cube << " current "
                    << (cur ? WideToUtf8(cur->folder.substr(cur->folder.find_last_of(L'\\') + 1)) : std::string("-"))
                    << (cur ? " avg " + std::to_string(cur->verification.avg) + " " + cur->verification.metric : std::string())
                    << " | latest " << (lat ? WideToUtf8(lat->folder.substr(lat->folder.find_last_of(L'\\') + 1)) : std::string("-"))
                    << " | meter " << rec.correctionMade << " due " << rec.reminderDue);
        }
    }
}

// ---- review fixes -----------------------------------------------------------------------------------

TEST_CASE("dlc join: an SDR-content check on an HDR display is not the HDR verification") {
    Data d = SampleData();
    Report sdrInHdr;   // newest, verifies the same HDR cube, but measured SDR content
    sdrInHdr.display = "Panel A"; sdrInHdr.mode = "HDR"; sdrInHdr.contentMode = "SDR"; sdrInHdr.date = "2026-05-30";
    sdrInHdr.verification.present = true;
    sdrInHdr.runtimeCube = L"X:\\a_hdr.cube";
    Report hdr = sdrInHdr;
    hdr.contentMode = "HDR"; hdr.date = "2026-05-20";
    d.reports.insert(d.reports.begin(), { sdrInHdr, hdr });
    LiveDisplay live;
    live.lut[1] = L"X:\\a_hdr.cube";
    const ModeRecord rec = RecordFor(d, "Panel A", 1, live, 90);
    CHECK(rec.current == 1);   // the HDR check, not the newer SDR-content one
    CHECK(rec.latest == 1);
}

TEST_CASE("dlc join: a cube counts as verified only over the MHC it was measured with") {
    Data d = SampleData();
    LiveDisplay a;
    a.lut[0] = d.stacks[0].cubePath;
    a.mhcSource[0] = d.stacks[0].baseLut;
    CHECK(RecordFor(d, "Panel A", 0, a, 90).current == 0);
    a.mhcSource[0] = L"X:\\dlc\\runs\\older\\generated\\mhc_base_sdr.cube";   // the MHC changed under the cube
    ModeRecord rec = RecordFor(d, "Panel A", 0, a, 90);
    CHECK(rec.mhc == LayerState::Differs);
    CHECK(rec.current == -1);
    // A verify-only report records the MHC it measured: that decides, not the registry.
    d.reports[0].verifiedMhcSource = a.mhcSource[0];
    rec = RecordFor(d, "Panel A", 0, a, 90);
    CHECK(rec.current == 0);
}

TEST_CASE("dlc join: contradicting hardware ids veto file evidence; twins on the desk are not guessed") {
    Data d = SampleData();
    // Panel B's verify-only report says it is QQQ1111; a BBB2222 display running Panel B's MHC is not it.
    Report idRep;
    idRep.display = "Panel B"; idRep.mode = "HDR"; idRep.hardwareId = "QQQ1111";
    d.reports.push_back(idRep);
    LiveDisplay other;
    other.hardwareId = "BBB2222";
    other.mhcSource[1] = d.stacks[1].baseLut;
    CHECK(MatchDisplay(d, other).name.empty());
    other.hardwareId = "QQQ1111";
    CHECK(MatchDisplay(d, other).name == "Panel B");

    // One profile entry with ABC1234 (monitor 0), two such panels live: index decides, else files, else ambiguous.
    LiveDisplay twin;
    twin.hardwareId = "ABC1234";
    twin.sameModelCount = 2;
    twin.monitor = 0;
    CHECK(MatchDisplay(d, twin).name == "Panel A");
    twin.monitor = 1;
    CHECK(MatchDisplay(d, twin).ambiguous);
    twin.lut[0] = d.stacks[0].cubePath;            // ...unless it runs Panel A's files
    CHECK(MatchDisplay(d, twin).name == "Panel A");
}

TEST_CASE("dlc: correction store schema 1 uses DLC's file-name tags, the SPD fallback, contradictions -> SDR") {
    CHECK(InferModeFromFilename(L"X:\\third_party\\BenQ-HDR.ccmx") == "HDR");
    CHECK(InferModeFromFilename(L"X:\\HDR\\plain.ccmx").empty());              // the folder does not count
    CHECK(InferModeFromFilename(L"panel_HDR_SDR.ccmx").empty());               // contradictory
    CHECK(InferModeFromFilename(L"panelhdr.ccmx").empty());                    // inside a word
    CHECK(InferModeFromFilename(L"panel.sdr") == "SDR");
    const auto v1 = ParseCorrectionStore(R"({"displays": {
        "Panel S": {"spd_file": "spd_HDR.sp", "correction_made": "2026-01-01"},
        "Panel M": {"mode": "hdr", "correction_file": "a_SDR.ccmx"},
        "Panel X": {"correction_file": "x_HDR_SDR.ccmx"}}})");
    REQUIRE(v1.size() == 3);
    CHECK(v1[0].mode == "HDR");   // no correction file: the SPD file's tag
    CHECK(v1[1].mode == "HDR");   // a recorded mode wins over the tag
    CHECK(v1[2].mode == "SDR");   // contradictory tags -> SDR
}

TEST_CASE("dlc: path normalisation and run ids") {
    CHECK(NormPath(L"X:\\dlc\\.\\results\\\\A\\") == L"x:\\dlc\\results\\a");
    CHECK(NormPath(L"X:\\dlc\\runs\\..\\results") == L"x:\\dlc\\results");
    CHECK(NormPath(L"\\\\server\\share\\dlc") == L"\\\\server\\share\\dlc");
    CHECK(NormPath(L"X:\\") == L"x:\\");
    CHECK(SamePath(L"X:/dlc/./results/A", L"x:\\DLC\\results\\a\\"));
    CHECK(RunIdFromPath(L"D:\\runs\\work\\DLC\\runs\\20260101_000000_sdr_a\\generated\\f.cube") == "20260101_000000_sdr_a");
}

TEST_CASE("dlc yaml: alternating nested sequences and mappings are depth-bounded, the rest still parses") {
    std::string deep;
    for (int i = 0; i < 400; i++) deep += std::string((size_t)i * 2, ' ') + "- a:\n";
    deep = "first: 1\nnest:\n" + deep + "last: 2\n";
    YamlNode root;
    CHECK_NOTHROW(root = ParseYaml(deep));
    CHECK(root.str("first") == "1");
    CHECK(root.str("last") == "2");
    CHECK(ParseYaml(std::string(kMaxYamlBytes + 1, 'a')).kind == YamlNode::Null);
}

TEST_CASE("dlc scan: unreadable reports are remembered until they change; cancel returns early") {
    TempDir t;
    t.write(L"calibration_profile.yaml", "paths:\n  output: results\n");
    t.mkdir(L"results\\Broken_2026-01-01_SDR");
    t.write(L"results\\Broken_2026-01-01_SDR\\report.json", "{not json");
    ScanCache cache;
    auto d = ScanFolder(t.path, cache, "2026-01-02");
    CHECK(d->reports.empty());
    REQUIRE(cache.reports.size() == 1);
    CHECK_FALSE(cache.reports.begin()->second.ok);
    t.write(L"results\\Broken_2026-01-01_SDR\\report.json",
            R"({"flow": "full", "mode": "SDR", "display": "Panel", "date": "2026-01-01", "verification": {}})");
    d = ScanFolder(t.path, cache, "2026-01-02");
    CHECK(d->reports.size() == 1);
    std::atomic<bool> cancel{ true };
    d = ScanFolder(t.path, cache, "2026-01-02", &cancel);
    CHECK(d->reports.empty());
}

