#include "dlc/dlc_data.h"

#include "dlc/dlc_yaml.h"
#include "ipc_json.h"

#include <windows.h>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <cwctype>
#include <algorithm>

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
    std::wstring s;
    s.reserve(p.size());
    for (wchar_t c : p) s += c == L'/' ? L'\\' : (wchar_t)std::towlower(c);
    // Keep a UNC "\\" or a drive "x:" head; normalise the segments after it.
    std::wstring head;
    size_t i = 0;
    bool rooted = false;
    if (s.compare(0, 2, L"\\\\") == 0) { head = L"\\\\"; i = 2; }
    else if (s.size() >= 2 && s[1] == L':') { head = s.substr(0, 2); i = 2; rooted = s.size() > 2 && s[2] == L'\\'; }
    else rooted = !s.empty() && s[0] == L'\\';
    std::vector<std::wstring> segs;
    while (i <= s.size()) {
        size_t j = s.find(L'\\', i);
        if (j == std::wstring::npos) j = s.size();
        const std::wstring seg = s.substr(i, j - i);
        if (seg == L"..") { if (!segs.empty()) segs.pop_back(); }
        else if (!seg.empty() && seg != L".") segs.push_back(seg);
        i = j + 1;
    }
    std::wstring out = head;
    if (rooted) out += L'\\';
    for (size_t k = 0; k < segs.size(); k++) {
        if (k > 0) out += L'\\';
        out += segs[k];
    }
    return out;
}

bool SamePath(const std::wstring& a, const std::wstring& b) {
    return !a.empty() && !b.empty() && NormPath(a) == NormPath(b);
}

std::string RunIdFromPath(const std::wstring& p) {
    const std::wstring n = NormPath(p);   // run ids are lower-case: the normalised spelling is the id
    std::wstring fallback, best;
    size_t at = 0;
    while ((at = n.find(L"\\runs\\", at)) != std::wstring::npos) {
        const size_t start = at + 6;
        const size_t end = n.find(L'\\', start);
        const std::wstring seg = n.substr(start, (end == std::wstring::npos ? n.size() : end) - start);
        if (!seg.empty()) {
            fallback = seg;
            if (IsRunFolderName(seg)) best = seg;
        }
        at = start;
    }
    return WideToUtf8(best.empty() ? fallback : best);
}

std::string InferModeFromFilename(const std::wstring& path) {
    const size_t slash = path.find_last_of(L"\\/");
    std::string base = WideToUtf8(slash == std::wstring::npos ? path : path.substr(slash + 1));
    for (char& c : base) c = (char)toupper((unsigned char)c);
    auto delim = [](char c) { return c == '_' || c == '-' || c == '.' || c == ' '; };
    bool hdr = false, sdr = false;
    for (size_t i = 0; i + 3 <= base.size(); i++) {
        const std::string tok = base.substr(i, 3);
        if (tok != "HDR" && tok != "SDR") continue;
        if (i > 0 && !delim(base[i - 1])) continue;
        if (i + 3 < base.size() && !delim(base[i + 3])) continue;
        (tok == "HDR" ? hdr : sdr) = true;
    }
    return hdr == sdr ? std::string() : (hdr ? "HDR" : "SDR");
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
            p.correctionFile = c->str("file");
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
    static const char* const kRecordKeys[] = { "display", "correction_file", "correction_made", "spd_file", "white_xy",
                                                "white_provenance", "observer", "anchor", "strength", "updated",
                                                "mode", "mode_source" };
    std::vector<CorrectionRecord> legacy;
    for (const auto& kv : displays->members) {
        const JsonValue& entry = kv.second;
        if (entry.type != JsonValue::Obj) continue;
        bool flat = false;
        for (const char* k : kRecordKeys) if (entry.has(k)) { flat = true; break; }
        if (flat) {
            // Schema 1 (DLC's _legacy_record): one slot. A recorded mode wins; else the correction file
            // name's tag, else the SPD file's; untagged or contradictory -> SDR.
            std::string mode = NormMode(Str(&entry, "mode"));
            if (mode.empty()) {
                const std::string file = Str(&entry, "correction_file");
                mode = InferModeFromFilename(Utf8ToWide(!file.empty() ? file : Str(&entry, "spd_file")));
            }
            take(kv.first, entry, mode.empty() ? "SDR" : mode);
            legacy.push_back(out.back());
            out.pop_back();
            continue;
        }
        for (const auto& mk : entry.members)   // schema 2: one record per mode
            if (mk.second.type == JsonValue::Obj && !NormMode(mk.first).empty()) take(kv.first, mk.second, NormMode(mk.first));
    }
    // A schema-2 slot wins over a legacy one for the same display and mode; among legacy ones, the first.
    for (const CorrectionRecord& l : legacy) {
        bool taken = false;
        for (const CorrectionRecord& r : out) if (r.display == l.display && r.mode == l.mode) taken = true;
        if (!taken) out.push_back(l);
    }
    return out;
}

namespace {

// A positive rational from a JSON number (an integer the double holds exactly).
bool Rational(const JsonValue* v, const char* key, uint32_t& out) {
    const double d = Num(v, key);
    if (!std::isfinite(d) || d < 1.0 || d > 4294967295.0 || std::floor(d) != d) return false;
    out = (uint32_t)d;
    return true;
}

std::string Upper(std::string s) {
    for (char& c : s) c = (char)toupper((unsigned char)c);
    return s;
}

// The catalogue's spelling of a rate: 3 decimals, trailing zeros dropped ("47.952", "60").
std::string HzKey(double hz) {
    char b[32];
    snprintf(b, sizeof(b), "%.3f", hz);
    std::string s = b;
    while (!s.empty() && s.back() == '0') s.pop_back();
    if (!s.empty() && s.back() == '.') s.pop_back();
    return s;
}

// One record's refresh_response (DLC dip.py RefreshResponse.from_dict's rules; anything it would refuse is an error
// here too, so DesktopLUT never derives a trim from a section DLC itself would drop).
DipRefreshSection ParseRefreshSection(const std::string& key, const JsonValue& rec, const JsonValue& rr) {
    DipRefreshSection s;
    s.recordKey = key;
    s.display = Str(&rec, "display");
    s.mode = NormMode(Str(&rec, "mode"));
    if (s.display.empty() || s.mode.empty()) {   // a key "<display>:<MODE>" names both
        const size_t colon = key.rfind(':');
        if (colon != std::string::npos) {
            if (s.display.empty()) s.display = key.substr(0, colon);
            if (s.mode.empty()) s.mode = NormMode(key.substr(colon + 1));
        } else if (s.display.empty()) {
            s.display = key;
        }
    }
    s.maxAgeDays = Int(&rec, "max_age_days", -1);
    auto fail = [&s](const std::string& why) { s.error = why; s.rates.clear(); return s; };
    if (rr.type != JsonValue::Obj) return fail("refresh_response is not an object");
    s.schema = Int(&rr, "schema", kDipRefreshSchema);
    if (s.schema > kDipRefreshSchema) return fail("schema " + std::to_string(s.schema) + " is newer than this build reads");
    s.measured = Str(&rr, "measured");
    if (s.measured.empty()) return fail("no measured date");
    s.runId = Str(&rr, "run_id");
    // The reference: recorded, or (no block) dip.py's default 60 / 1, not recorded. A block that names no positive
    // rational is unusable: the trim would not know what its ratios are against.
    if (const JsonValue* refv = rr.find("reference"); refv && refv->type != JsonValue::Null) {
        if (refv->type != JsonValue::Obj) return fail("the reference is not an object");
        uint32_t n = 0, d = 0;
        if (!Rational(refv, "num", n) || !Rational(refv, "den", d))
            return fail("the reference rate has no positive rational");
        s.referenceNum = n;
        s.referenceDen = d;
        s.referenceKey = Str(refv, "key").empty() ? HzKey((double)n / (double)d) : Str(refv, "key");
        s.referenceRecorded = true;
    }
    if (const JsonValue* v = Obj(&rr, "vrr")) {
        s.vrr.present = true;
        if (const JsonValue* range = v->find("range_hz"); range && range->type == JsonValue::Arr && range->arr.size() >= 2) {
            if (range->arr[0].type == JsonValue::Num) s.vrr.rangeLoHz = range->arr[0].num;
            if (range->arr[1].type == JsonValue::Num) s.vrr.rangeHiHz = range->arr[1].num;
        }
        if (const JsonValue* pts = v->find("points"); pts && pts->type == JsonValue::Arr) s.vrr.points = (int)pts->arr.size();
        if (const JsonValue* xc = Obj(v, "cross_check")) {
            const JsonValue* measuredv = xc->find("measured");
            const bool notMeasured = measuredv && measuredv->type == JsonValue::Bool && !measuredv->b;
            if (const JsonValue* ok = xc->find("ok"); ok && ok->type == JsonValue::Bool && !notMeasured)
                s.vrr.crossCheckOk = ok->b ? 1 : 0;
            s.vrr.crossCheckRate = Str(xc, "rate");
        }
        s.vrr.fixedModeTrimDerived = Bool(v, "fixed_mode_trim_derived");
    }
    if (const JsonValue* id = Obj(&rr, "identity")) {
        s.hardwareId = Upper(Str(id, "hardware_id"));
        s.devicePath = WStr(id, "device_path");
        s.edidId = Str(id, "edid_id");
    }
    s.peakNits = Num(&rr, "peak_nits");
    s.interpolable = Bool(&rr, "interpolable");
    const JsonValue* rates = rr.find("rates");
    if (!rates || rates->type != JsonValue::Arr) return fail("no rates");
    for (const JsonValue& r : rates->arr) {
        if (r.type != JsonValue::Obj) return fail("a rate is not an object");
        DipRefreshRate rate;
        rate.key = Str(&r, "key");
        if (rate.key.empty() || !Rational(&r, "num", rate.num) || !Rational(&r, "den", rate.den))
            return fail("a rate without a key or a positive rational");
        rate.hz = (double)rate.num / (double)rate.den;
        rate.sameAs = Str(&r, "same_as");
        if (!Str(&r, "source").empty()) rate.source = Str(&r, "source");
        if (const JsonValue* proof = Obj(&r, "proof")) {
            if (const JsonValue* ok = proof->find("ok"); ok && ok->type == JsonValue::Bool) rate.proofOk = ok->b ? 1 : 0;
            rate.proofKind = Str(proof, "kind");
        }
        if (const JsonValue* levels = r.find("levels"); levels && levels->type == JsonValue::Arr) {
            for (const JsonValue& l : levels->arr) {
                DipRefreshLevel lv;
                lv.nits = Num(&l, "nits");
                lv.ratio = Num(&l, "ratio");
                lv.uLn = Num(&l, "u_ln");
                lv.n = Int(&l, "n", 0);
                if (!(std::isfinite(lv.nits) && lv.nits > 0.0 && std::isfinite(lv.ratio) && lv.ratio > 0.0))
                    return fail("rate " + rate.key + ": a level without nits > 0 and ratio > 0");
                rate.levels.push_back(lv);
            }
        }
        std::sort(rate.levels.begin(), rate.levels.end(),
                  [](const DipRefreshLevel& a, const DipRefreshLevel& b) { return a.nits < b.nits; });
        s.rates.push_back(std::move(rate));
    }
    return s;
}

// ---- the record summary: DLC's own load rules (dip.py DisplayInstrumentProfile.from_dict) -------------------------
// Python truthiness of a JSON value: what `x or []` / `if d.get(k)` skip.
bool Falsy(const JsonValue& v) {
    switch (v.type) {
    case JsonValue::Null: return true;
    case JsonValue::Bool: return !v.b;
    case JsonValue::Num:  return v.num == 0.0;
    case JsonValue::Str:  return v.str.empty();
    case JsonValue::Arr:  return v.arr.empty();
    case JsonValue::Obj:  return v.members.empty();
    }
    return true;
}

// float(v): a number, a bool, or a string that is a whole number literal.
bool AsFloat(const JsonValue& v, double& out) {
    if (v.type == JsonValue::Num) { out = v.num; return true; }
    if (v.type == JsonValue::Bool) { out = v.b ? 1.0 : 0.0; return true; }
    if (v.type != JsonValue::Str) return false;
    const char* s = v.str.c_str();
    while (*s == ' ' || *s == '\t' || *s == '\n' || *s == '\r') s++;
    char* end = nullptr;
    out = strtod(s, &end);
    if (end == s) return false;
    while (*end == ' ' || *end == '\t' || *end == '\n' || *end == '\r') end++;
    return *end == '\0';
}

// int(v): a number (truncated), a bool, or a string holding an integer.
bool AsInt(const JsonValue& v, int& out) {
    double d = 0.0;
    if (v.type == JsonValue::Str) {
        const std::string& s = v.str;
        size_t i = 0;
        while (i < s.size() && s[i] == ' ') i++;
        if (i < s.size() && (s[i] == '+' || s[i] == '-')) i++;
        const size_t digits = i;
        while (i < s.size() && isdigit((unsigned char)s[i])) i++;
        size_t j = i;
        while (j < s.size() && s[j] == ' ') j++;
        if (i == digits || j != s.size()) return false;
        d = strtod(s.c_str(), nullptr);
    } else if (!AsFloat(v, d) || !std::isfinite(d)) {
        return false;
    }
    if (d > 2147483647.0 || d < -2147483648.0) return false;
    out = (int)std::trunc(d);
    return true;
}

// A float field: absent / null = unmeasured (NaN); otherwise it must convert, or DLC drops the record.
bool OptFloat(const JsonValue& rec, const char* key, double& out) {
    out = kNaN;
    const JsonValue* v = rec.find(key);
    if (!v || v->type == JsonValue::Null) return true;
    return AsFloat(*v, out);
}

bool OptInt(const JsonValue& rec, const char* key, int& out, int def) {
    out = def;
    const JsonValue* v = rec.find(key);
    if (!v || v->type == JsonValue::Null) return true;
    return AsInt(*v, out);
}

// Iterable of floats (`[float(c) for c in v]`), when truthy; `count` its length.
bool FloatList(const JsonValue* v, size_t& count) {
    count = 0;
    if (!v || Falsy(*v)) return true;
    if (v->type != JsonValue::Arr) return false;
    double d = 0.0;
    for (const JsonValue& c : v->arr) if (!AsFloat(c, d)) return false;
    count = v->arr.size();
    return true;
}

std::string OptStr(const JsonValue& rec, const char* key) {
    const JsonValue* v = rec.find(key);
    return v && v->type == JsonValue::Str ? v->str : std::string();
}

// ---- peak_by_window (dip.py PeakByWindow.from_dict's rules) ----
// _opt_pos: absent / null = NaN; otherwise a finite number >= 0, or the section is refused.
bool OptPos(const JsonValue& o, const char* key, double& out, std::string& err) {
    out = kNaN;
    const JsonValue* v = o.find(key);
    if (!v || v->type == JsonValue::Null) return true;
    double d = 0.0;
    if (!AsFloat(*v, d) || !std::isfinite(d)) { err = std::string(key) + " is not a finite number"; return false; }
    if (d < 0.0) { err = std::string(key) + " is negative"; return false; }
    out = d;
    return true;
}

// An entry of steps / curve: an object with area_pct in (0, 100] and its numbers non-negative.
bool PeakEntry(const JsonValue& e, const std::vector<const char*>& nums, double& area, std::string& err) {
    if (e.type != JsonValue::Obj) { err = "a peak_by_window entry is not an object"; return false; }
    const JsonValue* a = e.find("area_pct");
    if (!a || !AsFloat(*a, area) || !std::isfinite(area)) { err = "an entry without a finite area_pct"; return false; }
    if (!(area > 0.0 && area <= 100.0)) { err = "area_pct " + FormatSig(area, 4) + " outside (0, 100]"; return false; }
    double unused = 0.0;
    for (const char* k : nums) if (!OptPos(e, k, unused, err)) return false;
    return true;
}

DipPeakPass PeakPass(const JsonValue* p) {
    DipPeakPass out;
    if (!p || p->type != JsonValue::Obj) return out;
    out.present = true;
    std::string ignored;
    OptPos(*p, "early_nits", out.early, ignored);
    OptPos(*p, "sustained_nits", out.sustained, ignored);
    OptPos(*p, "min_nits", out.min, ignored);
    OptPos(*p, "tau_s", out.tau, ignored);
    if (const JsonValue* s = p->find("settled"); s && s->type == JsonValue::Bool) out.settled = s->b ? 1 : 0;
    return out;
}

DipPeakByWindow PeakByWindowFrom(const JsonValue* v) {
    DipPeakByWindow p;
    if (!v || v->type == JsonValue::Null) return p;
    p.present = true;
    auto fail = [&p](const std::string& why) { p.error = why; p.curve.clear(); return p; };
    if (v->type != JsonValue::Obj) return fail("peak_by_window is not an object");
    p.measured = Str(v, "measured");
    if (const JsonValue* sv = v->find("schema")) {   // int(d.get("schema", 1)): a null or a word is refused
        if (!AsInt(*sv, p.schema)) return fail("the schema is not a number");
    } else {
        p.schema = kDipPeakWindowSchema;
    }
    if (p.schema > kDipPeakWindowSchema) { p.newer = true; return p; }
    if (!v->find("measured") || v->find("measured")->type == JsonValue::Null) return fail("no measured date");
    p.runId = Str(v, "run_id");
    std::string err;
    static const std::vector<const char*> kStepNums = { "early_nits", "early_t_s", "sustained_nits", "min_nits",
                                                                  "max_nits", "tau_s", "hold_s", "cooldown_s",
                                                                  "black_nits", "t_start_s" };
    static const std::vector<const char*> kCurveNums = { "early_nits", "sustained_nits", "min_nits", "tau_s" };
    if (const JsonValue* steps = v->find("steps"); steps && !Falsy(*steps)) {
        if (steps->type != JsonValue::Arr) return fail("steps is not a list");
        for (const JsonValue& s : steps->arr) {
            double area = 0.0;
            if (!PeakEntry(s, kStepNums, area, err)) return fail(err);
        }
        p.steps = (int)steps->arr.size();
    }
    const JsonValue* curve = v->find("curve");
    if (curve && !Falsy(*curve) && curve->type != JsonValue::Arr) return fail("curve is not a list");
    if (!curve || Falsy(*curve)) return fail("no curve");
    for (const JsonValue& c : curve->arr) {
        DipPeakSize z;
        if (!PeakEntry(c, kCurveNums, z.areaPct, err)) return fail(err);
        OptPos(c, "early_nits", z.early, err);
        OptPos(c, "sustained_nits", z.sustained, err);
        OptPos(c, "min_nits", z.min, err);
        OptPos(c, "tau_s", z.tau, err);
        z.up = PeakPass(c.find("up"));
        z.down = PeakPass(c.find("down"));
        if (const JsonValue* ag = Obj(&c, "agreement")) {
            if (const JsonValue* ok = ag->find("ok"); ok && ok->type == JsonValue::Bool) z.agree = ok->b ? 1 : 0;
            z.relDiff = Num(ag, "rel_diff");
            z.tol = Num(ag, "tol");
        }
        z.n = Int(&c, "n", 0);
        p.curve.push_back(z);
    }
    std::stable_sort(p.curve.begin(), p.curve.end(), [](const DipPeakSize& a, const DipPeakSize& b) { return a.areaPct < b.areaPct; });
    if (!OptPos(*v, "plateau_nits", p.plateauNits, err) || !OptPos(*v, "run_window_area_pct", p.runWindowAreaPct, err) ||
        !OptPos(*v, "run_window_sustained_nits", p.runWindowSustainedNits, err) ||
        !OptPos(*v, "full_field_sustained_nits", p.fullFieldSustainedNits, err))
        return fail(err);
    for (const char* k : { "agreement", "identity", "conditions", "rules", "wear" })   // dict(x or {})
        if (const JsonValue* o = v->find(k); o && !Falsy(*o) && o->type != JsonValue::Obj) return fail(std::string(k) + " is not an object");
    if (const JsonValue* notes = v->find("notes"); notes && !Falsy(*notes) && notes->type != JsonValue::Arr &&
                                                    notes->type != JsonValue::Str && notes->type != JsonValue::Obj)
        return fail("notes is not a list");
    p.valid = Bool(v, "valid");
    if (const JsonValue* ag = Obj(v, "agreement")) {
        if (const JsonValue* failed = ag->find("areas_failed"); failed && failed->type == JsonValue::Arr)
            for (const JsonValue& a : failed->arr) if (a.type == JsonValue::Num) p.areasFailed.push_back(a.num);
        p.systematic = Bool(ag, "systematic");
        p.maxRelDiff = Num(ag, "max_rel_diff");
    }
    if (const JsonValue* cond = Obj(v, "conditions")) {
        p.stack = Str(cond, "stack");
        if (const JsonValue* e = cond->find("emissive"); e && e->type == JsonValue::Bool) p.emissive = e->b ? 1 : 0;
    }
    if (const JsonValue* wear = Obj(v, "wear")) {
        p.peakWhiteS = Num(wear, "peak_white_s");
        p.longestHoldS = Num(wear, "longest_hold_s");
    }
    return p;
}

DipPeakByWindow ParsePeakByWindow(const JsonValue& rec) { return PeakByWindowFrom(rec.find("peak_by_window")); }

// False: DLC's loader would refuse the record (a field that does not convert), so it is dropped here too.
bool ParseDipRecord(const std::string& key, const JsonValue& rec, DipRecord& r) {
    r.key = key;
    r.display = Str(&rec, "display");
    std::string mode = Str(&rec, "mode");
    if (r.display.empty()) {   // DLC: the key names the display when the record does not
        const size_t colon = key.rfind(':');
        r.display = colon != std::string::npos && !NormMode(key.substr(colon + 1)).empty() ? key.substr(0, colon) : key;
    }
    r.mode = NormMode(mode);
    if (r.mode.empty() && !mode.empty()) r.mode = mode;   // kept as written; it then matches no live mode
    // noise_model: each band needs float nits and sigma_de.
    if (const JsonValue* nm = rec.find("noise_model"); nm && !Falsy(*nm)) {
        if (nm->type != JsonValue::Arr) return false;
        for (const JsonValue& b : nm->arr) {
            double d = 0.0;
            if (b.type != JsonValue::Obj || !b.find("nits") || !b.find("sigma_de") || !AsFloat(*b.find("nits"), d) ||
                !AsFloat(*b.find("sigma_de"), d))
                return false;
            if (const JsonValue* sr = b.find("sigma_rel"); sr && sr->type != JsonValue::Null && !AsFloat(*sr, d)) return false;
        }
        r.noiseBands = (int)nm->arr.size();
    }
    double unused = 0.0;
    if (!OptFloat(rec, "noise_floor_nits", r.noiseFloorNits) || !OptFloat(rec, "read_overhead_s", r.readOverheadS) ||
        !OptFloat(rec, "settle_seconds", r.settleS) || !OptFloat(rec, "native_white_nits", r.whiteNits) ||
        !OptFloat(rec, "sustained_peak_nits", r.sustainedPeakNits) || !OptFloat(rec, "native_black_nits", r.blackNits) ||
        !OptFloat(rec, "eotf_undershoot", r.eotfUndershoot) || !OptFloat(rec, "warmup_minutes", r.warmupMinutes) ||
        !OptFloat(rec, "fluctuation_envelope", unused) || !OptFloat(rec, "warmin_magnitude", unused) ||
        !OptFloat(rec, "creep_rate_de_per_min", r.creepDePerMin) || !OptFloat(rec, "recommended_drift_threshold", unused) ||
        !OptFloat(rec, "patch_window_area_pct", unused))
        return false;
    int ignored = 0;
    if (!OptInt(rec, "warmup_reads_to_settle", ignored, 0) || !OptInt(rec, "thermal_tau_patches", ignored, 0) ||
        !OptInt(rec, "recommended_neutral_interval", ignored, 0) || !OptInt(rec, "max_age_days", r.maxAgeDays, -1))
        return false;
    // settle_by_level / native_primaries: dict(x) when truthy.
    for (const char* k : { "settle_by_level", "native_primaries" })
        if (const JsonValue* v = rec.find(k); v && !Falsy(*v) && v->type != JsonValue::Obj) return false;
    if (const JsonValue* sbl = Obj(&rec, "settle_by_level")) {
        double d = 0.0;
        if (const JsonValue* b = sbl->find("bright"); b && AsFloat(*b, d)) r.settleBrightS = d;
        if (const JsonValue* k = sbl->find("dark"); k && AsFloat(*k, d)) r.settleDarkS = d;
    }
    if (const JsonValue* pr = Obj(&rec, "native_primaries")) {
        auto xy = [pr](const char* c) {
            const JsonValue* v = pr->find(c);
            return v && v->type == JsonValue::Arr && v->arr.size() >= 2 && v->arr[0].type == JsonValue::Num &&
                   v->arr[1].type == JsonValue::Num;
        };
        r.hasPrimaries = xy("R") && xy("G") && xy("B");
    }
    // native_white_xy: [float(v[0]), float(v[1])] when truthy.
    if (const JsonValue* w = rec.find("native_white_xy"); w && !Falsy(*w)) {
        if (w->type != JsonValue::Arr || w->arr.size() < 2 || !AsFloat(w->arr[0], r.whiteX) || !AsFloat(w->arr[1], r.whiteY))
            return false;
    }
    // white_vs_luminance: rows of floats; warm_balance: floats.
    if (const JsonValue* wl = rec.find("white_vs_luminance"); wl && !Falsy(*wl)) {
        if (wl->type != JsonValue::Arr) return false;
        size_t n = 0;
        for (const JsonValue& row : wl->arr) if (!FloatList(&row, n) || row.type != JsonValue::Arr) return false;
        r.whiteVsLuminance = (int)wl->arr.size();
    }
    size_t n = 0;
    if (!FloatList(rec.find("warm_balance"), n)) return false;
    if (const JsonValue* notes = rec.find("notes"); notes && !Falsy(*notes) && notes->type != JsonValue::Arr &&
                                                      notes->type != JsonValue::Str && notes->type != JsonValue::Obj)
        return false;   // list(x): any iterable
    r.thermalRegime = OptStr(rec, "thermal_regime");
    r.coldChannel = OptStr(rec, "cold_channel");
    r.instrument = OptStr(rec, "instrument");
    r.correctionFile = OptStr(rec, "correction_file");
    r.made = OptStr(rec, "made");
    r.updated = OptStr(rec, "updated");
    r.peakByWindow = ParsePeakByWindow(rec);
    return true;
}

}  // namespace

std::string FormatSig(double v, int sig, bool sign) {
    if (!std::isfinite(v)) return {};
    if (v == 0.0) return "0";
    sig = std::clamp(sig, 1, 12);
    const double a = std::fabs(v);
    // Decimals so the first significant digit and sig - 1 after it show; none for whole numbers past 10^sig.
    int decimals = sig - 1 - (int)std::floor(std::log10(a));
    decimals = std::clamp(decimals, 0, 15);
    char b[64];
    snprintf(b, sizeof(b), "%.*f", decimals, a);
    // A value that rounded up into the next decade (0.00999 at 2 -> "0.010"): the trailing zeros go below.
    std::string s = b;
    if (s.find('.') != std::string::npos) {
        while (!s.empty() && s.back() == '0') s.pop_back();
        if (!s.empty() && s.back() == '.') s.pop_back();
    }
    // Never "0" for a value that is not zero (decimals are bounded at 15: below 1e-15 shows in exponent form).
    if (s == "0") {
        snprintf(b, sizeof(b), "%.*e", (std::max)(0, sig - 1), a);
        s = b;
    }
    if (v < 0.0) s = "-" + s;
    else if (sign) s = "+" + s;
    return s;
}

DipPeakByWindow ParsePeakByWindowJson(const std::string& json) {
    JsonValue root;
    if (!ParseJson(json, root)) {
        DipPeakByWindow p;
        p.present = true;
        p.error = "not JSON";
        return p;
    }
    return PeakByWindowFrom(&root);
}

double DipContrast(const DipRecord& r) {
    if (!(std::isfinite(r.whiteNits) && r.whiteNits > 0.0 && std::isfinite(r.blackNits) && r.blackNits > 0.0)) return kNaN;
    return r.whiteNits / r.blackNits;
}

int DipAgeDays(const DipRecord& r, const std::string& today) {
    return r.made.empty() ? -1 : DaysBetween(r.made, today);
}

bool DipStale(const DipRecord& r, const std::string& today) {
    if (r.made.empty() || DaysBetween(r.made, r.made) != 0) return true;   // no readable `made`: stale, as DLC says
    if (DaysBetween(today, today) != 0) return true;                       // DLC: an unreadable date is stale
    return DaysBetween(r.made, today) > (r.maxAgeDays >= 0 ? r.maxAgeDays : kDipDefaultMaxAgeDays);
}

DipStoreData ParseDipStore(const std::string& json) {
    DipStoreData out;
    JsonValue root;
    if (!ParseJson(json, root) || root.type != JsonValue::Obj) return out;
    const JsonValue* displays = Obj(&root, "displays");
    if (!displays) return out;
    out.readable = true;
    for (const auto& kv : displays->members) {
        if (kv.second.type != JsonValue::Obj) {   // DLC cannot read it either
            out.dropped.push_back(kv.first);
            continue;
        }
        int refresh = -1;
        if (const JsonValue* rr = kv.second.find("refresh_response"); rr && rr->type != JsonValue::Null) {
            refresh = (int)out.refresh.size();
            out.refresh.push_back(ParseRefreshSection(kv.first, kv.second, *rr));
        }
        DipRecord r;
        if (!ParseDipRecord(kv.first, kv.second, r)) {
            out.dropped.push_back(kv.first);
            continue;
        }
        r.refresh = refresh;
        out.records.push_back(std::move(r));
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
        out.verifiedMhcSource = WStr(Obj(Obj(vo, "measured_stack"), "installed_stack"), "pipe_source_file");
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
