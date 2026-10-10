#include "dlc/dlc_data.h"

#include "dlc/dlc_yaml.h"
#include "ipc_json.h"

#include <windows.h>
#include <cmath>
#include <cwctype>

namespace dlc {

using ipc_json::JsonOptions;
using ipc_json::JsonParser;
using ipc_json::JsonValue;

namespace {

bool ParseJson(const std::string& text, JsonValue& out) {
    try {
        JsonParser p(text, JsonOptions::Files());
        out = p.parse();
        return true;
    } catch (...) {
        return false;
    }
}

const JsonValue* Obj(const JsonValue* v, const char* key) {
    const JsonValue* c = v ? v->find(key) : nullptr;
    return c && c->type == JsonValue::Obj ? c : nullptr;
}

std::string Str(const JsonValue* v, const char* key) {
    const JsonValue* c = v ? v->find(key) : nullptr;
    return c && c->type == JsonValue::Str ? c->str : std::string();
}

std::wstring WStr(const JsonValue* v, const char* key) { return Utf8ToWide(Str(v, key)); }

double Num(const JsonValue* v, const char* key) {
    const JsonValue* c = v ? v->find(key) : nullptr;
    return c && c->type == JsonValue::Num ? c->num : kNaN;
}

int Int(const JsonValue* v, const char* key, int def) {
    const double d = Num(v, key);
    return std::isfinite(d) ? (int)std::llround(d) : def;
}

bool Bool(const JsonValue* v, const char* key) {
    const JsonValue* c = v ? v->find(key) : nullptr;
    return c && c->type == JsonValue::Bool && c->b;
}

void Triple(const JsonValue* arr, double out[3]) {
    if (!arr || arr->type != JsonValue::Arr) return;
    for (size_t k = 0; k < 3 && k < arr->arr.size(); k++)
        out[k] = arr->arr[k].type == JsonValue::Num ? arr->arr[k].num : kNaN;
}

bool IsAbsolute(const std::wstring& p) {
    return (p.size() >= 2 && p[1] == L':') || (p.size() >= 2 && (p[0] == L'\\' || p[0] == L'/') && (p[1] == L'\\' || p[1] == L'/'));
}

}  // namespace

std::wstring Utf8ToWide(const std::string& s) {
    if (s.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring w((size_t)(n > 0 ? n : 0), L'\0');
    if (n > 0) MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), w.data(), n);
    return w;
}

std::string WideToUtf8(const std::wstring& w) {
    if (w.empty()) return {};
    const int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string s((size_t)(n > 0 ? n : 0), '\0');
    if (n > 0) WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), s.data(), n, nullptr, nullptr);
    return s;
}

std::wstring NormPath(const std::wstring& p) {
    std::wstring out;
    out.reserve(p.size());
    for (wchar_t c : p) out += c == L'/' ? L'\\' : (wchar_t)std::towlower(c);
    while (out.size() > 3 && out.back() == L'\\') out.pop_back();
    return out;
}

bool SamePath(const std::wstring& a, const std::wstring& b) {
    return !a.empty() && !b.empty() && NormPath(a) == NormPath(b);
}

std::string RunIdFromPath(const std::wstring& p) {
    const std::wstring n = NormPath(p);
    const size_t at = n.find(L"\\runs\\");
    if (at == std::wstring::npos) return {};
    const size_t start = at + 6;
    const size_t end = n.find(L'\\', start);
    // The original casing: run ids are lower-case anyway, but take it from the input.
    return WideToUtf8(p.substr(start, (end == std::wstring::npos ? p.size() : end) - start));
}

std::string NormMode(const std::string& m) {
    if (m.size() != 3) return {};
    std::string u;
    for (char c : m) u += (char)toupper((unsigned char)c);
    return u == "SDR" || u == "HDR" ? u : std::string();
}

int DaysBetween(const std::string& isoDate, const std::string& today) {
    auto parse = [](const std::string& s, SYSTEMTIME& st) {
        int y = 0, m = 0, d = 0;
        if (s.size() < 10 || sscanf_s(s.c_str(), "%4d-%2d-%2d", &y, &m, &d) != 3) return false;
        if (y < 1900 || m < 1 || m > 12 || d < 1 || d > 31) return false;
        st = {};
        st.wYear = (WORD)y; st.wMonth = (WORD)m; st.wDay = (WORD)d;
        return true;
    };
    SYSTEMTIME a, b;
    FILETIME fa, fb;
    if (!parse(isoDate, a) || !parse(today, b) || !SystemTimeToFileTime(&a, &fa) || !SystemTimeToFileTime(&b, &fb))
        return -1;
    const ULONGLONG ta = ((ULONGLONG)fa.dwHighDateTime << 32) | fa.dwLowDateTime;
    const ULONGLONG tb = ((ULONGLONG)fb.dwHighDateTime << 32) | fb.dwLowDateTime;
    const long long day = 10000000LL * 86400;
    return (int)(((long long)tb - (long long)ta) / day);
}

Profile ParseProfile(const std::string& yamlText, const std::wstring& profileDir) {
    Profile p;
    const YamlNode root = ParseYaml(yamlText);
    if (root.kind != YamlNode::Map) {
        p.outputDir = profileDir + L"\\results";
        return p;
    }
    p.readable = true;
    if (const YamlNode* ds = root.find("displays"); ds && ds->kind == YamlNode::Seq) {
        for (const YamlNode& d : ds->seq) {
            if (d.kind != YamlNode::Map) continue;
            ProfileDisplay pd;
            pd.name = d.str("name");
            if (pd.name.empty()) continue;
            pd.monitor = d.integer("desktoplut_monitor", -1);
            if (const YamlNode* q = d.find("quirks"); q && q->kind == YamlNode::Map) {
                pd.hardwareId = q->str("hardware_id");
                pd.shortName = q->str("short_name");
            }
            p.displays.push_back(pd);
        }
    }
    std::wstring out = L"results";
    if (const YamlNode* paths = root.find("paths"); paths && paths->kind == YamlNode::Map) {
        const std::string o = paths->str("output");
        if (!o.empty()) out = Utf8ToWide(o);
    }
    p.outputDir = IsAbsolute(out) ? out : profileDir + L"\\" + out;
    if (const YamlNode* meter = root.find("meter"); meter && meter->kind == YamlNode::Map) {
        if (const YamlNode* c = meter->find("correction"); c && c->kind == YamlNode::Map) {
            p.correctionMade = c->str("made");
            p.correctionMaxAgeDays = c->integer("max_age_days", 180);
            if (p.correctionMaxAgeDays <= 0) p.correctionMaxAgeDays = 180;
        }
    }
    return p;
}

std::vector<StackRecord> ParseStackRegistry(const std::string& json) {
    std::vector<StackRecord> out;
    JsonValue root;
    if (!ParseJson(json, root)) return out;
    const JsonValue* stacks = Obj(&root, "stacks");
    if (!stacks) return out;
    for (const auto& kv : stacks->members) {
        const JsonValue& r = kv.second;
        if (r.type != JsonValue::Obj) continue;
        StackRecord s;
        s.display = Str(&r, "display");
        s.mode = NormMode(Str(&r, "mode"));
        if (s.display.empty() || s.mode.empty()) continue;   // DLC drops these too
        s.monitor = Int(&r, "monitor", -1);
        s.runId = Str(&r, "run_id");
        s.appliedAt = Str(&r, "applied_at");
        s.baseLut = WStr(Obj(&r, "mhc"), "base_lut");
        if (const JsonValue* cube = Obj(&r, "cube")) {
            s.cubePath = WStr(cube, "cube_path");
            s.cubeRunId = Str(cube, "run_id");
            s.cubeAppliedAt = Str(cube, "applied_at");
        }
        out.push_back(std::move(s));
    }
    return out;
}

std::vector<CorrectionRecord> ParseCorrectionStore(const std::string& json) {
    std::vector<CorrectionRecord> out;
    JsonValue root;
    if (!ParseJson(json, root)) return out;
    const JsonValue* displays = Obj(&root, "displays");
    if (!displays) return out;
    auto take = [&](const std::string& name, const JsonValue& r, std::string mode) {
        CorrectionRecord c;
        c.display = Str(&r, "display");
        if (c.display.empty()) c.display = name;
        c.mode = NormMode(Str(&r, "mode"));
        if (c.mode.empty()) c.mode = mode;
        c.made = Str(&r, "correction_made");
        c.file = WStr(&r, "correction_file");
        out.push_back(std::move(c));
    };
    for (const auto& kv : displays->members) {
        const JsonValue& entry = kv.second;
        if (entry.type != JsonValue::Obj) continue;
        if (entry.has("display") || entry.has("correction_file")) {
            // Schema 1: one flat record per display. Its mode is the _HDR / _SDR token in the file name,
            // else SDR (what DLC assumes).
            const std::string file = Str(&entry, "correction_file");
            std::string upper;
            for (char ch : file) upper += (char)toupper((unsigned char)ch);
            take(kv.first, entry, upper.find("_HDR") != std::string::npos ? "HDR" : "SDR");
            continue;
        }
        for (const auto& mk : entry.members)   // schema 2: one record per mode
            if (mk.second.type == JsonValue::Obj && !NormMode(mk.first).empty()) take(kv.first, mk.second, NormMode(mk.first));
    }
    return out;
}

bool ParseReport(const std::string& json, Report& out) {
    JsonValue root;
    if (!ParseJson(json, root) || root.type != JsonValue::Obj) return false;
    out.display = Str(&root, "display");
    out.mode = NormMode(Str(&root, "mode"));
    out.contentMode = NormMode(Str(&root, "content_mode"));
    out.flow = Str(&root, "flow");
    out.date = Str(&root, "date");
    out.target = Str(&root, "target");
    out.monitor = Int(&root, "monitor", -1);
    if (out.display.empty() || out.mode.empty()) return false;   // not a run report (the stage CLI writes another shape)
    if (const JsonValue* v = Obj(&root, "verification")) {
        Verification& ver = out.verification;
        ver.present = true;
        ver.metric = Str(v, "metric");
        if (ver.metric.empty()) ver.metric = "CIEDE2000";
        ver.avg = Num(v, "avg_de2000");
        ver.p95 = Num(v, "p95_de2000");
        ver.max = Num(v, "max_de2000");
        ver.white = Num(v, "white_de2000");
        ver.grayAvg = Num(v, "grayscale_avg_de2000");
        ver.patchCount = Int(v, "patch_count", 0);
        ver.withinQuality = Bool(v, "within_quality");
        if (const JsonValue* worst = v->find("worst"); worst && worst->type == JsonValue::Arr) {
            for (const JsonValue& w : worst->arr) {
                WorstPatch wp;
                double rgb[3] = { kNaN, kNaN, kNaN };
                Triple(w.find("rgb"), rgb);
                for (int k = 0; k < 3; k++) wp.rgb[k] = (float)rgb[k];
                wp.de = Num(&w, "de2000");
                out.worst.push_back(wp);
            }
        }
    }
    out.cubePath = WStr(Obj(&root, "lut3d"), "cube_path");
    out.deliveredCube = WStr(Obj(&root, "deliverables"), "cube");
    if (const JsonValue* vo = Obj(&root, "verify_only")) {
        out.runtimeCube = WStr(Obj(vo, "measured_stack"), "runtime_cube");
        out.hardwareId = Str(Obj(Obj(vo, "verify_source"), "identity"), "hardware_id");
    }
    return true;
}

const char* MetricLabel(const std::string& metric) {
    if (metric == "dE_ITP") return "\xCE\x94" "E ITP";
    return "\xCE\x94" "E2000";
}

std::vector<Patch> ParsePatchMetrics(const std::string& json) {
    std::vector<Patch> out;
    JsonValue root;
    if (!ParseJson(json, root) || root.type != JsonValue::Arr) return out;
    out.reserve(root.arr.size());
    for (const JsonValue& r : root.arr) {
        if (r.type != JsonValue::Obj) continue;
        Patch p;
        double rgb[3] = { kNaN, kNaN, kNaN };
        Triple(r.find("rgb"), rgb);
        if (!std::isfinite(rgb[0]) || !std::isfinite(rgb[1]) || !std::isfinite(rgb[2])) continue;
        for (int k = 0; k < 3; k++) p.rgb[k] = (float)rgb[k];
        Triple(r.find("measured_xyz"), p.measured);
        Triple(r.find("target_xyz"), p.target);
        p.de = Num(&r, "de2000");
        p.grayscale = Bool(&r, "grayscale");
        p.clamped = Bool(&r, "gamut_clamped");
        out.push_back(p);
    }
    return out;
}

std::vector<std::wstring> RunDoneResultsDirs(const std::string& eventsTail) {
    std::vector<std::wstring> out;
    size_t pos = 0;
    while ((pos = eventsTail.find("\"run_done\"", pos)) != std::string::npos) {
        const size_t lineStart = eventsTail.rfind('\n', pos);
        const size_t a = lineStart == std::string::npos ? 0 : lineStart + 1;
        size_t b = eventsTail.find('\n', pos);
        if (b == std::string::npos) b = eventsTail.size();
        pos = b;
        JsonValue ev;
        if (!ParseJson(eventsTail.substr(a, b - a), ev)) continue;   // a line cut by the tail window
        if (Str(&ev, "event") != "run_done") continue;
        const std::wstring dir = WStr(Obj(&ev, "data"), "results_dir");
        if (!dir.empty()) out.push_back(dir);
    }
    return out;
}

bool IsRunFolderName(const std::wstring& n) {
    // YYYYMMDD_HHMMSS[_ffffff]_<sdr|hdr>_
    auto digits = [&](size_t at, size_t count) {
        if (at + count > n.size()) return false;
        for (size_t k = 0; k < count; k++) if (!iswdigit(n[at + k])) return false;
        return true;
    };
    if (!digits(0, 8) || n.size() < 16 || n[8] != L'_' || !digits(9, 6) || n[15] != L'_') return false;
    size_t at = 16;
    if (digits(16, 6) && n.size() > 22 && n[22] == L'_') at = 23;
    const std::wstring rest = n.substr(at);
    return rest.rfind(L"sdr_", 0) == 0 || rest.rfind(L"hdr_", 0) == 0;
}

}  // namespace dlc
