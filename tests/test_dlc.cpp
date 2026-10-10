#include "doctest.h"
#include "dlc/dlc_data.h"
#include "dlc/dlc_join.h"
#include "dlc/dlc_scan.h"
#include "dlc/dlc_yaml.h"
#include "ipc_json.h"

#include <windows.h>
#include <cmath>
#include <cstdio>
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
    CHECK(off.correctionMade == "2026-01-10");                   // no store entry: the profile's date
    CHECK(off.reminderDue);                                      // the meter correction is still stale
    Data fresh = d;
    fresh.profile.correctionMade.clear();
    CHECK_FALSE(RecordFor(fresh, "Panel B", 1, b, 0).reminderDue);

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
    CHECK(ResolveFolder(L"X:\\configured", L"") == L"X:\\configured");
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
    CHECK(d->folder == folder);
}
