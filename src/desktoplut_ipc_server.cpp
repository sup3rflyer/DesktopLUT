// DesktopLUT - desktoplut_ipc_server.cpp
// See desktoplut_ipc_server.h for the security model.

#include "desktoplut_ipc_server.h"

#include <windows.h>
#include <sddl.h>

#include <atomic>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "types.h"
#include "globals.h"
#include "gui_mhc.h"
#include "gui_shared.h"
#include "mhc.h"
#include "displayconfig.h"
#include "settings.h"
#include "processing.h"
#include "gui.h"
#include "dwm_inject.h"

#pragma comment(lib, "Advapi32.lib")

namespace {

const wchar_t* kPipeName = L"\\\\.\\pipe\\DesktopLUT.Calibration";
constexpr size_t kMaxRequestBytes = 256 * 1024;  // DoS guard
constexpr DWORD kGuiTimeoutMs = 60000;           // MHC install can be slow

// ===========================================================================
// UTF-8 <-> wide
// ===========================================================================
std::string WideToUtf8(const std::wstring& w) {
    if (w.empty()) return {};
    int len = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string out(len, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), out.data(), len, nullptr, nullptr);
    return out;
}

std::wstring Utf8ToWide(const std::string& s) {
    if (s.empty()) return {};
    int len = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    std::wstring out(len, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), out.data(), len);
    return out;
}

// ===========================================================================
// Minimal, self-contained JSON (no external dependency)
// ===========================================================================
struct JsonValue {
    enum Type { Null, Bool, Num, Str, Arr, Obj } type = Null;
    bool b = false;
    double num = 0.0;
    std::string str;
    std::vector<JsonValue> arr;
    std::vector<std::pair<std::string, JsonValue>> members;

    const JsonValue* find(const std::string& key) const {
        if (type != Obj) return nullptr;
        for (const auto& kv : members)
            if (kv.first == key) return &kv.second;
        return nullptr;
    }
    bool has(const std::string& key) const { return find(key) != nullptr; }
    std::string getStr(const std::string& key, const std::string& def = "") const {
        const JsonValue* v = find(key);
        return (v && v->type == Str) ? v->str : def;
    }
    double getNum(const std::string& key, double def = 0.0) const {
        const JsonValue* v = find(key);
        return (v && v->type == Num) ? v->num : def;
    }
    int getInt(const std::string& key, int def = 0) const {
        const JsonValue* v = find(key);
        return (v && v->type == Num) ? (int)std::llround(v->num) : def;
    }
    void set(const std::string& key, JsonValue v) { members.emplace_back(key, std::move(v)); }
};

JsonValue JBool(bool v) { JsonValue j; j.type = JsonValue::Bool; j.b = v; return j; }
JsonValue JNum(double v) { JsonValue j; j.type = JsonValue::Num; j.num = v; return j; }
JsonValue JStr(const std::string& v) { JsonValue j; j.type = JsonValue::Str; j.str = v; return j; }
JsonValue JObj() { JsonValue j; j.type = JsonValue::Obj; return j; }
JsonValue JArr() { JsonValue j; j.type = JsonValue::Arr; return j; }

void AppendUtf8(std::string& out, unsigned cp) {
    if (cp <= 0x7F) {
        out += (char)cp;
    } else if (cp <= 0x7FF) {
        out += (char)(0xC0 | (cp >> 6));
        out += (char)(0x80 | (cp & 0x3F));
    } else if (cp <= 0xFFFF) {
        out += (char)(0xE0 | (cp >> 12));
        out += (char)(0x80 | ((cp >> 6) & 0x3F));
        out += (char)(0x80 | (cp & 0x3F));
    } else {
        out += (char)(0xF0 | (cp >> 18));
        out += (char)(0x80 | ((cp >> 12) & 0x3F));
        out += (char)(0x80 | ((cp >> 6) & 0x3F));
        out += (char)(0x80 | (cp & 0x3F));
    }
}

struct JsonParser {
    const std::string& s;
    size_t i = 0;
    explicit JsonParser(const std::string& str) : s(str) {}

    [[noreturn]] void err(const char* m) { throw std::runtime_error(m); }
    void ws() {
        while (i < s.size() && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) i++;
    }
    JsonValue parse() { ws(); JsonValue v = value(); return v; }

    JsonValue value() {
        ws();
        if (i >= s.size()) err("unexpected end of input");
        char c = s[i];
        if (c == '{') return object();
        if (c == '[') return array();
        if (c == '"') return JStr(string());
        if (c == 't') { literal("true"); return JBool(true); }
        if (c == 'f') { literal("false"); return JBool(false); }
        if (c == 'n') { literal("null"); return JsonValue(); }
        return number();
    }
    void literal(const char* lit) {
        for (const char* p = lit; *p; ++p) {
            if (i >= s.size() || s[i] != *p) err("invalid literal");
            i++;
        }
    }
    unsigned hex4() {
        if (i + 4 > s.size()) err("bad \\u escape");
        unsigned v = 0;
        for (int k = 0; k < 4; ++k) {
            char c = s[i++];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= (c - '0');
            else if (c >= 'a' && c <= 'f') v |= (c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= (c - 'A' + 10);
            else err("bad hex digit");
        }
        return v;
    }
    std::string string() {
        if (s[i] != '"') err("expected string");
        i++;
        std::string out;
        while (i < s.size()) {
            char c = s[i++];
            if (c == '"') return out;
            if (c == '\\') {
                if (i >= s.size()) err("bad escape");
                char e = s[i++];
                switch (e) {
                    case '"': out += '"'; break;
                    case '\\': out += '\\'; break;
                    case '/': out += '/'; break;
                    case 'n': out += '\n'; break;
                    case 't': out += '\t'; break;
                    case 'r': out += '\r'; break;
                    case 'b': out += '\b'; break;
                    case 'f': out += '\f'; break;
                    case 'u': {
                        unsigned cp = hex4();
                        if (cp >= 0xD800 && cp <= 0xDBFF && i + 1 < s.size() && s[i] == '\\' && s[i + 1] == 'u') {
                            i += 2;
                            unsigned lo = hex4();
                            if (lo >= 0xDC00 && lo <= 0xDFFF)
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        }
                        AppendUtf8(out, cp);
                        break;
                    }
                    default: err("bad escape");
                }
            } else {
                out += c;
            }
        }
        err("unterminated string");
    }
    JsonValue number() {
        size_t start = i;
        if (i < s.size() && s[i] == '-') i++;
        while (i < s.size() &&
               ((s[i] >= '0' && s[i] <= '9') || s[i] == '.' || s[i] == 'e' || s[i] == 'E' || s[i] == '+' || s[i] == '-'))
            i++;
        if (i == start) err("invalid number");
        return JNum(std::strtod(s.substr(start, i - start).c_str(), nullptr));
    }
    JsonValue array() {
        JsonValue v = JArr();
        i++;  // [
        ws();
        if (i < s.size() && s[i] == ']') { i++; return v; }
        while (true) {
            v.arr.push_back(value());
            ws();
            if (i >= s.size()) err("unterminated array");
            if (s[i] == ',') { i++; continue; }
            if (s[i] == ']') { i++; break; }
            err("expected , or ]");
        }
        return v;
    }
    JsonValue object() {
        JsonValue v = JObj();
        i++;  // {
        ws();
        if (i < s.size() && s[i] == '}') { i++; return v; }
        while (true) {
            ws();
            std::string key = string();
            ws();
            if (i >= s.size() || s[i] != ':') err("expected :");
            i++;
            v.members.emplace_back(key, value());
            ws();
            if (i >= s.size()) err("unterminated object");
            if (s[i] == ',') { i++; continue; }
            if (s[i] == '}') { i++; break; }
            err("expected , or }");
        }
        return v;
    }
};

void SerializeStr(const std::string& s, std::string& out) {
    out += '"';
    for (unsigned char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += (char)c;
                }
        }
    }
    out += '"';
}

void Serialize(const JsonValue& v, std::string& out) {
    switch (v.type) {
        case JsonValue::Null: out += "null"; break;
        case JsonValue::Bool: out += v.b ? "true" : "false"; break;
        case JsonValue::Num: {
            char buf[40];
            std::snprintf(buf, sizeof(buf), "%.10g", v.num);
            out += buf;
            break;
        }
        case JsonValue::Str: SerializeStr(v.str, out); break;
        case JsonValue::Arr: {
            out += '[';
            for (size_t k = 0; k < v.arr.size(); ++k) { if (k) out += ','; Serialize(v.arr[k], out); }
            out += ']';
            break;
        }
        case JsonValue::Obj: {
            out += '{';
            for (size_t k = 0; k < v.members.size(); ++k) {
                if (k) out += ',';
                SerializeStr(v.members[k].first, out);
                out += ':';
                Serialize(v.members[k].second, out);
            }
            out += '}';
            break;
        }
    }
}

std::string OkResponse(const JsonValue& result) {
    JsonValue env = JObj();
    env.set("ok", JBool(true));
    env.set("result", result);
    std::string out;
    Serialize(env, out);
    return out;
}
std::string ErrResponse(const std::string& error) {
    JsonValue env = JObj();
    env.set("ok", JBool(false));
    env.set("error", JStr(error));
    std::string out;
    Serialize(env, out);
    return out;
}

std::vector<float> ReadFloatArray(const JsonValue* v) {
    std::vector<float> out;
    if (v && v->type == JsonValue::Arr)
        for (const auto& e : v->arr)
            if (e.type == JsonValue::Num) out.push_back((float)e.num);
    return out;
}

// ===========================================================================
// Calibration-mode bookkeeping (module-local; own mutex)
// ===========================================================================
struct CalibState {
    bool active = false;
    int monitor = -1;
    std::wstring mode;  // L"SDR" / L"HDR"
    std::wstring dummyIcc;
    std::wstring reason;
    bool correctionsReset = false;
    bool hasSnapshot = false;
    int snapMonitor = -1;
    bool snapWasHdr = false;
    MonitorSettings snapshot;
};
std::mutex g_calibMutex;
CalibState g_calib;

// Marshaling envelope: pipe thread -> GUI thread.
struct CalibGuiRequest {
    const std::string* method;
    const JsonValue* params;
    JsonValue* result;
    std::string* error;
};

// ---- shared helpers --------------------------------------------------------
bool ParseMonitorMode(const JsonValue& p, int& mon, bool& isHDR, std::string& error) {
    if (!p.has("monitor")) { error = "missing parameter: monitor"; return false; }
    mon = p.getInt("monitor", -1);
    std::string mode = p.getStr("mode");
    if (mode != "SDR" && mode != "HDR") { error = "mode must be SDR or HDR"; return false; }
    isHDR = (mode == "HDR");
    std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
    if (mon < 0 || mon >= (int)g_gui.monitorSettings.size()) { error = "monitor index out of range"; return false; }
    return true;
}

std::string MonitorModeKey(int mon, bool isHDR) {
    return std::to_string(mon) + (isHDR ? ":HDR" : ":SDR");
}

// ===========================================================================
// Correction-grayscale live preview (mhc.grayscale_live_*)
// ===========================================================================
// Drives DesktopLUT's main-GUI correction-grayscale editor (the "Edit Points"
// live-edit) over the pipe so DLC can automate the end-of-run grayscale touch-up:
// engage a preview of MHCSettings::correctionGrayscale on top of the existing
// MHC + 3D-LUT stack (measurable by the meter), nudge it per patch, then bake it
// into the ICC ("OK") or revert ("Cancel"). It edits ONLY correctionGrayscale —
// never the matrix, baseGrayscale, primaries/white, or the 3D LUT — and is
// one-toggle revertible to the vanilla core ICM. This mirrors the GUI handler at
// gui.cpp ID_MHC_SDR_GS_EDIT, just split across begin/set/commit/cancel calls so
// a tiny per-(monitor,mode) record carries savedPerm and the pre-begin correction
// across the calls. Guarded by g_monitorSettingsMutex.
struct GsLiveState {
    bool active = false;
    uint8_t savedPerm = 0;                 // active permutation before PERM_GS was stripped
    bool startedForPreview = false;        // this preview started full processing
    bool startedOverlayForPreview = false; // this preview started the DWM-hook overlay
    GrayscaleSettings savedCorrectionGs;   // pre-begin correctionGrayscale, for cancel/abort restore
    std::wstring sdrPassthroughName;       // realization-A: transient identity scanout profile (SDR full-preview)
};
std::map<std::pair<int, bool>, GsLiveState> g_gsLive;  // keyed by (monitor, isHDR)

// Tear down an active grayscale live preview. bake=true regenerates the MHC ICC
// with the previewed correctionGrayscale baked in (commit / "OK"); bake=false
// restores correctionGrayscale to its pre-begin value first, so the regen reverts
// to the vanilla core (cancel / abort). RegenerateMhcIfActive recomputes the active
// permutation from the (now-restored or now-final) settings — so it re-includes
// PERM_GS on commit and drops it on cancel without an explicit perm swap, exactly
// as the GUI editor's close path relies on. Must be called WITHOUT
// g_monitorSettingsMutex held (RegenerateMhcIfActive locks it internally).
void FinishGsLive(int mon, bool isHDR, const GsLiveState& st, bool bake) {
    g_mhcEditDialogOpen.store(false);  // re-arm MHC profile monitoring (suppressed during preview)
    {
        std::lock_guard<std::mutex> lk(g_monitorsMutex);
        for (auto& ctx : g_monitors)
            if (ctx.index == mon) {
                ctx.corrGsPreviewActive = false;
                ctx.corrGsFullPreviewActive = false;   // realization-A full-preview off
                ctx.cbDirty = true;
                break;
            }
    }
    if (!bake) {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        if (mon >= 0 && mon < (int)g_gui.monitorSettings.size()) {
            MHCSettings& m = isHDR ? g_gui.monitorSettings[mon].hdrMHC
                                   : g_gui.monitorSettings[mon].sdrMHC;
            m.correctionGrayscale = st.savedCorrectionGs;
        }
    }
    // Bake the (restored or final) correctionGrayscale into the ICC and restore the
    // full permutation. No-op if MHC isn't active (no profileName).
    RegenerateMhcIfActive(mon, isHDR);
    UpdateMhcFlagsLive(mon);
    // realization-A: now the real profile is re-associated, drop the transient passthrough
    // (remove association + delete the .icm). Done AFTER the real reassoc to avoid a no-profile flash.
    if (!st.sdrPassthroughName.empty()) DisengageSdrPassthroughScanout(mon, st.sdrPassthroughName);
    // If the overlay was already running (we didn't spin it up), flush the transient
    // preview push by re-queuing the REAL shader CC (correctionGrayscale lives in the
    // ICC now, not the shader). UpdateColorCorrectionLive reads sdr/hdrColorCorrection,
    // whose grayscale is off in the calibration stack — so this clears the preview.
    if (!st.startedForPreview) UpdateColorCorrectionLive(mon, isHDR);
    if (st.startedForPreview) StopProcessing();
    if (st.startedOverlayForPreview) DwmHookReevaluateOverlay();
    SaveSettings();
}

// Abort any active grayscale live preview (e.g. the client died between begin and
// commit, leaving corrGsPreviewActive set + PERM_GS stripped). Reverts to vanilla.
// Called from calibration.exit / corrections.disable_all. Snapshots + clears g_gsLive
// under the lock, then runs teardown OUTSIDE it (FinishGsLive locks internally).
void CleanupActiveGsLive() {
    std::vector<std::pair<std::pair<int, bool>, GsLiveState>> pending;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        for (auto& kv : g_gsLive)
            if (kv.second.active) pending.push_back(kv);
        g_gsLive.clear();
    }
    for (auto& kv : pending)
        FinishGsLive(kv.first.first, kv.first.second, kv.second, /*bake=*/false);
}

bool AnyCorrectionActive() {
    std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
    for (const auto& s : g_gui.monitorSettings) {
        if (!s.sdrPath.empty() || !s.hdrPath.empty() ||
            s.sdrMHC.enabled || s.hdrMHC.enabled ||
            s.sdrColorCorrection.primariesEnabled || s.sdrColorCorrection.grayscale.enabled ||
            s.hdrColorCorrection.primariesEnabled || s.hdrColorCorrection.grayscale.enabled ||
            s.hdrColorCorrection.tonemap.enabled)
            return true;
    }
    return false;
}

// Restart the overlay/hook processing so cleared/loaded LUTs and shader flags
// take effect, mirroring what the GUI's Apply path does.
void ReapplyProcessing() {
    StopProcessing();
    if (AnyCorrectionActive()) StartProcessing();
}

constexpr int kMaxMhcGrayscalePoints = 32;

std::vector<float> ResampleUniform(const std::vector<float>& src, int count, float fallback) {
    std::vector<float> out((std::max)(count, 0), fallback);
    if (count <= 0 || src.empty()) return out;
    if ((int)src.size() == count) return src;
    if (src.size() == 1 || count == 1) {
        std::fill(out.begin(), out.end(), src.front());
        return out;
    }
    float denom = (float)(count - 1);
    float srcMax = (float)(src.size() - 1);
    for (int i = 0; i < count; ++i) {
        float pos = ((float)i / denom) * srcMax;
        int i0 = (int)floorf(pos);
        int i1 = (std::min)(i0 + 1, (int)src.size() - 1);
        float t = pos - floorf(pos);
        out[i] = src[i0] + (src[i1] - src[i0]) * t;
    }
    return out;
}

void ApplyGrayscalePayload(GrayscaleSettings& gs, const JsonValue& p) {
    int pc = p.getInt("point_count", 0);
    std::vector<float> pts = ReadFloatArray(p.find("points"));
    const JsonValue* dev = p.find("deviations");
    std::vector<float> r = ReadFloatArray(dev ? dev->find("r") : nullptr);
    std::vector<float> g = ReadFloatArray(dev ? dev->find("g") : nullptr);
    std::vector<float> b = ReadFloatArray(dev ? dev->find("b") : nullptr);
    // Decomposed editor sliders (optional): luminance[] is the common (main) slider
    // per point, rgb{r,g,b} the per-channel balance strips; deviations stays the
    // composed back-compat form (luminance*rgb). When present, the decomposition is
    // authoritative: luminance scales the points curve — exactly what the editor's
    // main slider edits — and rgb lands on rgbDeviations, so opening the editor shows
    // the solver's split instead of a zero main slider with common-mode R/G/B.
    std::vector<float> lum = ReadFloatArray(p.find("luminance"));
    const JsonValue* rgb = p.find("rgb");
    std::vector<float> balR = ReadFloatArray(rgb ? rgb->find("r") : nullptr);
    std::vector<float> balG = ReadFloatArray(rgb ? rgb->find("g") : nullptr);
    std::vector<float> balB = ReadFloatArray(rgb ? rgb->find("b") : nullptr);
    if (pc <= 0) pc = (int)pts.size();
    if (pc <= 0) pc = (int)gs.points.size();
    if (pc <= 0) pc = 20;
    if ((int)pts.size() != pc) {
        pts.assign(pc, 0.0f);
        for (int k = 0; k < pc; ++k) pts[k] = (pc > 1) ? (float)k / (pc - 1) : 0.0f;
    }
    auto fix = [pc](std::vector<float>& v) { if ((int)v.size() != pc) v.assign(pc, 1.0f); };
    fix(r); fix(g); fix(b);
    bool haveLum = (int)lum.size() == pc;
    bool haveRgb = (int)balR.size() == pc && (int)balG.size() == pc && (int)balB.size() == pc;
    if (haveLum && !haveRgb) {
        // Luminance without balance: recover the balance from the composed deviations
        // (deviations = luminance*rgb) so the split still lands on the right controls.
        balR.assign(pc, 1.0f); balG.assign(pc, 1.0f); balB.assign(pc, 1.0f);
        for (int k = 0; k < pc; ++k) {
            float l = lum[k];
            if (fabsf(l) > 1e-6f) { balR[k] = r[k] / l; balG[k] = g[k] / l; balB[k] = b[k] / l; }
        }
        haveRgb = true;
    }
    int dstPc = std::clamp(pc, 2, kMaxMhcGrayscalePoints);
    gs.pointCount = dstPc;
    gs.points = ResampleUniform(pts, dstPc, 0.0f);
    if (haveLum) {
        std::vector<float> lumR = ResampleUniform(lum, dstPc, 1.0f);
        for (int k = 0; k < dstPc; ++k) gs.points[k] *= lumR[k];
    }
    gs.rgbDeviations[0] = ResampleUniform(haveRgb ? balR : r, dstPc, 1.0f);
    gs.rgbDeviations[1] = ResampleUniform(haveRgb ? balG : g, dstPc, 1.0f);
    gs.rgbDeviations[2] = ResampleUniform(haveRgb ? balB : b, dstPc, 1.0f);
    gs.enabled = true;
}

// ===========================================================================
// Read-only handlers (served on the pipe thread)
// ===========================================================================
void HandleStateGet(JsonValue& result) {
    result.set("running", JBool(g_running.load() || g_gui.isRunning.load()));
    // WIRE CONTRACT (consumed by DLC). Mirrors the OVERLAY-active flag, NOT the DWM-hook state:
    // reads false in hook mode even while a cube is live. Judge hook-mode liveness by cube_path
    // + hook health, not this field. See docs/NAMING.md §4.
    result.set("corrections_enabled", JBool(g_shaderCorrectionsActive.load()));
    {
        std::lock_guard<std::mutex> lk(g_calibMutex);
        if (g_calib.active) {
            JsonValue cm = JObj();
            cm.set("active", JBool(true));
            cm.set("monitor", JNum(g_calib.monitor));
            cm.set("mode", JStr(WideToUtf8(g_calib.mode)));
            cm.set("dummy_icc_path", JStr(WideToUtf8(g_calib.dummyIcc)));
            cm.set("corrections_reset", JBool(g_calib.correctionsReset));
            result.set("calibration_mode", cm);
        } else {
            result.set("calibration_mode", JsonValue());
        }
    }
    JsonValue mhc = JObj();
    JsonValue runtime = JObj();
    JsonValue layers = JObj();
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        for (size_t idx = 0; idx < g_gui.monitorSettings.size(); ++idx) {
            const MonitorSettings& s = g_gui.monitorSettings[idx];
            for (int mode = 0; mode < 2; ++mode) {
                bool isHDR = (mode == 1);
                const MHCSettings& m = isHDR ? s.hdrMHC : s.sdrMHC;
                std::string key = MonitorModeKey((int)idx, isHDR);
                if (m.enabled && !m.profileName.empty()) {
                    JsonValue e = JObj();
                    e.set("applied", JBool(true));
                    e.set("profile_name", JStr(WideToUtf8(m.profileName)));
                    // The DLC-owned base artifact the profile was generated from (a 1D .cube
                    // handed over set_base_lut). Stable across the WB/DG/GS permutation
                    // re-bakes that churn profile_name — the identity a client keys on.
                    if (!m.sourceFilePath.empty())
                        e.set("source_file", JStr(WideToUtf8(m.sourceFilePath)));
                    e.set("active_perm", JNum(m.activePerm));
                    mhc.set(key, e);
                }
                const std::wstring& path = isHDR ? s.hdrPath : s.sdrPath;
                if (!path.empty()) {
                    JsonValue e = JObj();
                    e.set("cube_path", JStr(WideToUtf8(path)));
                    runtime.set(key, e);
                }
                // Viewing layers (the GUI toggles a calibration must measure WITHOUT): the
                // MHC's white balance / correction grayscale / Desktop Gamma (HDR) permutation
                // bits and the HDR tonemap shader flag. Reported for every pair so a client can
                // capture the user's state before a run and restore it after (layers.set).
                JsonValue l = JObj();
                l.set("white_balance", JBool(m.whiteBalanceEnabled));
                l.set("grayscale", JBool(m.correctionGrayscale.enabled));
                l.set("desktop_gamma", JBool(isHDR && m.desktopGammaEnabled));
                l.set("tonemap", JBool(isHDR && s.hdrColorCorrection.tonemap.enabled));
                if (isHDR) {
                    l.set("tonemap_dynamic", JBool(s.hdrColorCorrection.tonemap.dynamicPeak));
                    l.set("tonemap_target_peak", JNum(s.hdrColorCorrection.tonemap.targetPeakNits));
                }
                layers.set(key, l);
            }
        }
    }
    result.set("mhc", mhc);
    result.set("runtime", runtime);
    result.set("layers", layers);
}

void HandleCalibStatus(JsonValue& result) {
    std::lock_guard<std::mutex> lk(g_calibMutex);
    result.set("active", JBool(g_calib.active));
    if (g_calib.active) {
        JsonValue st = JObj();
        st.set("monitor", JNum(g_calib.monitor));
        st.set("mode", JStr(WideToUtf8(g_calib.mode)));
        st.set("dummy_icc_path", JStr(WideToUtf8(g_calib.dummyIcc)));
        st.set("corrections_reset", JBool(g_calib.correctionsReset));
        result.set("state", st);
    } else {
        result.set("state", JsonValue());
    }
}

void HandleQueryProfiles(const JsonValue& p, JsonValue& result) {
    // v1: DLC performs the authoritative Windows ICC audit via Argyll; here we
    // only echo the request and report the active device default if cheap.
    result.set("available", JBool(false));
    result.set("profiles", JArr());
    result.set("active_profile", JsonValue());
    if (p.has("monitor")) result.set("monitor", JNum(p.getInt("monitor", 0)));
    result.set("note", JStr("use Argyll dispwin for authoritative VCGT/profile state"));
}

void HandleQueryGammaRamp(const JsonValue& p, JsonValue& result) {
    int mon = p.has("monitor") ? p.getInt("monitor", 0) : 0;
    result.set("monitor", JNum(mon));
    HMONITOR hmon = nullptr;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        if (mon >= 0 && mon < (int)g_gui.monitors.size()) hmon = g_gui.monitors[mon];
    }
    auto unavailable = [&]() {
        result.set("available", JBool(false));
        result.set("gamma_ramp_loaded", JsonValue());
        result.set("vcgt_present", JsonValue());
    };
    if (!hmon) { unavailable(); return; }
    MONITORINFOEXW mi;
    mi.cbSize = sizeof(mi);
    if (!GetMonitorInfoW(hmon, &mi)) { unavailable(); return; }
    HDC hdc = CreateDCW(mi.szDevice, mi.szDevice, nullptr, nullptr);
    if (!hdc) { unavailable(); return; }
    WORD ramp[3][256];
    BOOL got = GetDeviceGammaRamp(hdc, ramp);
    DeleteDC(hdc);
    if (!got) { unavailable(); return; }
    bool identity = true;
    for (int c = 0; c < 3 && identity; ++c) {
        for (int k = 0; k < 256; ++k) {
            int expect = k * 257;
            if (expect > 65535) expect = 65535;
            int diff = (int)ramp[c][k] - expect;
            if (diff < 0) diff = -diff;
            if (diff > 384) { identity = false; break; }  // ~0.6% tolerance
        }
    }
    result.set("available", JBool(true));
    result.set("gamma_ramp_loaded", JBool(!identity));
    result.set("vcgt_present", JBool(!identity));
}

// Enumerate DesktopLUT monitors with enough identity for DLC to map a
// DesktopLUT monitor index -> an Argyll DISPLAY -> the physical panel
// deterministically (device name, position, primary flag, EDID hardware id,
// and the live color space SDR/ACM/HDR). Read-only: snapshot the HMONITOR +
// friendly name under g_monitorSettingsMutex, then run the (thread-safe)
// display query APIs OUTSIDE the lock — mirrors HandleQueryGammaRamp and keeps
// the slow DXGI work off the settings mutex.
void HandleQueryMonitors(const JsonValue& /*p*/, JsonValue& result) {
    struct MonSnap { HMONITOR hmon; std::wstring friendly; };
    std::vector<MonSnap> snaps;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        snaps.reserve(g_gui.monitors.size());
        for (size_t i = 0; i < g_gui.monitors.size(); ++i) {
            MonSnap s;
            s.hmon = g_gui.monitors[i];
            s.friendly = (i < g_gui.monitorNames.size()) ? g_gui.monitorNames[i] : std::wstring();
            snaps.push_back(std::move(s));
        }
    }

    JsonValue arr = JArr();
    for (size_t i = 0; i < snaps.size(); ++i) {
        JsonValue e = JObj();
        e.set("index", JNum((double)i));
        e.set("friendly_name", JStr(WideToUtf8(snaps[i].friendly)));

        if (snaps[i].hmon) {
            MONITORINFOEXW mi;
            mi.cbSize = sizeof(mi);
            if (GetMonitorInfoW(snaps[i].hmon, &mi)) {
                // GDI device name (\\.\DISPLAYn) — the Argyll display enumeration order.
                e.set("device_name", JStr(WideToUtf8(mi.szDevice)));
                JsonValue rect = JObj();
                rect.set("x", JNum((double)mi.rcMonitor.left));
                rect.set("y", JNum((double)mi.rcMonitor.top));
                rect.set("width", JNum((double)(mi.rcMonitor.right - mi.rcMonitor.left)));
                rect.set("height", JNum((double)(mi.rcMonitor.bottom - mi.rcMonitor.top)));
                e.set("rect", rect);
                e.set("primary", JBool((mi.dwFlags & MONITORINFOF_PRIMARY) != 0));
            }
        }

        DisplayInfo di;
        if (GetDisplayInfoForMonitor((int)i, di)) {
            e.set("device_path", JStr(WideToUtf8(di.devicePath)));
            e.set("hardware_id", JStr(WideToUtf8(ExtractHardwareIdFromPath(di.devicePath))));
            e.set("source_id", JNum((double)di.sourceId));
            e.set("target_id", JNum((double)di.targetId));
            e.set("hdr_capable", JBool(di.isHdrCapable));
            JsonValue adapter = JObj();
            adapter.set("low", JNum((double)di.adapterId.LowPart));
            adapter.set("high", JNum((double)di.adapterId.HighPart));
            e.set("adapter_id", adapter);
        }

        // Live color space via a fresh DXGI query (the same check the capture
        // path uses) so DLC can confirm the monitor's current mode before a run.
        if (snaps[i].hmon) {
            DXGI_OUTPUT_DESC1 desc;
            if (QueryFreshOutputDesc(snaps[i].hmon, desc)) {
                bool hdrActive = (desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020);
                bool acmSdr = (desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709);
                e.set("hdr_active", JBool(hdrActive));
                e.set("color_space", JStr(hdrActive ? "HDR" : (acmSdr ? "ACM_SDR" : "SDR")));
            }
        }

        arr.arr.push_back(std::move(e));
    }
    result.set("available", JBool(true));
    result.set("count", JNum((double)snaps.size()));
    result.set("monitors", arr);
}

// Switch a monitor between SDR and HDR over the wire — the same OS advanced-color
// flip the HDR-toggle hotkey performs, but targeted at an explicit monitor and an
// explicit desired state (so DLC can drive the SDR<->HDR characterize/calibrate
// modes without the operator touching Windows Settings). Params: monitor (int,
// required) + enable (bool, optional — absent means toggle). Idempotent: a no-op
// when already in the requested state. Resolution/read/set are thread-agnostic
// DisplayConfig calls (the same ones HandleQueryMonitors uses off-thread), so this
// runs off the GUI thread; DesktopLUT's own mode-switch MHC reapply fires
// independently off the WM_DISPLAYCHANGE the flip generates.
void HandleSetHdr(const JsonValue& p, JsonValue& result, std::string& error) {
    if (!p.has("monitor")) { error = "missing parameter: monitor"; return; }
    int mon = p.getInt("monitor", -1);
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        if (mon < 0 || mon >= (int)g_gui.monitorSettings.size()) {
            error = "monitor index out of range"; return;
        }
    }

    DisplayInfo di;
    if (!GetDisplayInfoForMonitor(mon, di)) {
        error = "could not resolve display for monitor"; return;
    }
    result.set("monitor", JNum((double)mon));
    result.set("hdr_capable", JBool(di.isHdrCapable));

    bool current = false;
    if (!GetDisplayHdrState(di, current)) {
        error = "could not read current HDR state"; return;
    }
    result.set("was_active", JBool(current));

    // Target: explicit `enable` (accept bool or 0/1 number), else toggle.
    const JsonValue* en = p.find("enable");
    bool target;
    if (en && en->type == JsonValue::Bool) target = en->b;
    else if (en && en->type == JsonValue::Num) target = (en->num != 0.0);
    else target = !current;  // toggle

    if (target && !di.isHdrCapable) { error = "monitor does not support HDR"; return; }

    bool changed = false;
    if (target != current) {
        if (!SetDisplayHdrState(di, target)) { error = "SetDisplayHdrState failed"; return; }
        changed = true;
    }

    // Re-read so the result reflects the authoritative resulting state, not intent.
    bool now = target;
    GetDisplayHdrState(di, now);
    result.set("now_active", JBool(now));
    result.set("changed", JBool(changed));
}

// ===========================================================================
// Mutating handlers (run on the GUI thread via WM_CALIB_CMD)
// ===========================================================================
void DoEnterNeutral(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    std::wstring dummy = Utf8ToWide(p.getStr("dummy_icc_path"));
    std::wstring reason = Utf8ToWide(p.getStr("reason"));

    DisplayInfo di;
    bool haveDi = GetDisplayInfoForMonitor(mon, di);

    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MonitorSettings& ms = g_gui.monitorSettings[mon];
        {
            std::lock_guard<std::mutex> ck(g_calibMutex);
            g_calib.snapshot = ms;  // snapshot BEFORE clearing
            g_calib.hasSnapshot = true;
            g_calib.snapMonitor = mon;
            g_calib.snapWasHdr = isHDR;
        }
        MHCSettings& mhc = isHDR ? ms.hdrMHC : ms.sdrMHC;
        if (haveDi && mhc.enabled && !mhc.profileName.empty())
            RemoveMHC2Profile(mhc.profileName, di.adapterId, di.sourceId, isHDR);
        mhc.enabled = false;
        // Clean MHC slate for a calibration build. A DLC run defines exactly what it
        // wants over the pipe (set_primaries / set_white / set_base_grayscale), so any
        // stale manual MHC correction left in the GUI must NOT bake into the new profile.
        // The pre-clear snapshot above preserves all of this for restore on revert/fail.
        //  * white balance: a leftover enabled WB silently shifts the matrix' white
        //    (this is exactly what contaminated an early DLC run).
        //  * base/correction grayscale: disabled so a stale curve can't ride along.
        //  * source ICC/1D-cube import: while set, primaries+TRC come from the FILE and
        //    the DLC's set_primaries is ignored — clear it so the manual path is used.
        mhc.whiteBalanceEnabled = false;
        mhc.baseGrayscale.enabled = false;
        mhc.correctionGrayscale.enabled = false;
        mhc.sourceFilePath.clear();
        mhc.hasPerChannelTRC = false;
        mhc.sourceIs1DCube = false;
        mhc.desktopGammaEnabled = false;
        // Clear the runtime 3D LUT + shader correction layers for the CALIBRATED mode
        // only. The other mode's layers are inert while the display is in the calibrated
        // mode, and clearing them here destroyed them on the apply path: exit with
        // restore_snapshot=false keeps the cleared state, so an HDR calibration
        // permanently dropped the user's SDR runtime cube (DLC field report 2026-08-14).
        if (isHDR) {
            ms.hdrPath.clear();
            ms.hdrColorCorrection.primariesEnabled = false;
            ms.hdrColorCorrection.grayscale.enabled = false;
            ms.hdrColorCorrection.tonemap.enabled = false;
        } else {
            ms.sdrPath.clear();
            ms.sdrColorCorrection.primariesEnabled = false;
            ms.sdrColorCorrection.grayscale.enabled = false;
        }
    }
    SaveSettings();
    UpdateMhcFlagsLive(mon);
    ReapplyProcessing();
    // NOTE: dummy-ICC association is deferred to live bring-up; neutrality here
    // comes from MHC removal + cleared layers, plus DLC's own `dispwin -c`.

    {
        std::lock_guard<std::mutex> ck(g_calibMutex);
        g_calib.active = true;
        g_calib.monitor = mon;
        g_calib.mode = isHDR ? L"HDR" : L"SDR";
        g_calib.dummyIcc = dummy;
        g_calib.reason = reason;
        g_calib.correctionsReset = true;
    }
    result.set("active", JBool(true));
    result.set("snapshot_id", JStr("calib-snapshot"));
    result.set("monitor", JNum(mon));
    result.set("mode", JStr(isHDR ? "HDR" : "SDR"));
    result.set("dummy_icc_path", JStr(WideToUtf8(dummy)));
    result.set("corrections_reset", JBool(true));
}

void DoExitCalibration(const JsonValue& p, JsonValue& result, std::string& error) {
    // Tear down any grayscale live preview left engaged (client died mid-edit) so
    // the panel doesn't stay stuck in preview with PERM_GS stripped.
    CleanupActiveGsLive();
    bool restore = false;
    const JsonValue* rv = p.find("restore_snapshot");
    if (rv && rv->type == JsonValue::Bool) restore = rv->b;
    bool restored = false;
    if (restore) {
        std::lock_guard<std::mutex> ck(g_calibMutex);
        if (g_calib.hasSnapshot && g_calib.snapMonitor >= 0) {
            {
                std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
                if (g_calib.snapMonitor < (int)g_gui.monitorSettings.size())
                    g_gui.monitorSettings[g_calib.snapMonitor] = g_calib.snapshot;
            }
            SaveSettings();
            // Reinstall the original MHC for the captured mode if it was active.
            MHCSettings& m = g_calib.snapWasHdr ? g_calib.snapshot.hdrMHC : g_calib.snapshot.sdrMHC;
            if (m.enabled) GenerateAndInstallMhcProfile(g_calib.snapMonitor, g_calib.snapWasHdr);
            UpdateMhcFlagsLive(g_calib.snapMonitor);
            ReapplyProcessing();
            restored = true;
        }
    }
    {
        std::lock_guard<std::mutex> ck(g_calibMutex);
        g_calib.active = false;
        g_calib.correctionsReset = false;
    }
    result.set("active", JBool(false));
    result.set("restored", JBool(restored));
}

// layers.set — toggle the viewing layers of one monitor:mode over the pipe, exactly as the
// GUI checkboxes do (DLC captures them before a run, measures with them OFF, restores after;
// the user should never have to manage corrections around a pipeline run). Params: monitor,
// mode, and any of white_balance / grayscale / desktop_gamma / tonemap (bool; omitted = keep).
//   * white_balance / grayscale / desktop_gamma live in the MHC layer: set the flags, then ONE
//     RegenerateMhcIfActive (BuildMHC2Params bakes all three), like ID_MHC_*_WB_ENABLE /
//     ID_MHC_*_GS_ENABLE. Desktop Gamma also drives the live gamma atomics (ID_MHC_HDR_DG_ENABLE).
//   * tonemap (HDR) is the shader flag: ID_CORR_TONEMAP_ENABLE's live update path.
// Result: {monitor_mode, before:{...}, after:{...}, regenerated, profile_name}.
static void LayersJson(const MonitorSettings& s, bool isHDR, JsonValue& out) {
    const MHCSettings& m = isHDR ? s.hdrMHC : s.sdrMHC;
    out.set("white_balance", JBool(m.whiteBalanceEnabled));
    out.set("grayscale", JBool(m.correctionGrayscale.enabled));
    out.set("desktop_gamma", JBool(isHDR && m.desktopGammaEnabled));
    out.set("tonemap", JBool(isHDR && s.hdrColorCorrection.tonemap.enabled));
}

void DoLayersSet(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    auto want = [&](const char* name, bool& has, bool& val) {
        const JsonValue* v = p.find(name);
        has = (v && v->type == JsonValue::Bool);
        if (has) val = v->b;
    };
    bool hasWb, wb, hasGs, gs, hasDg, dg, hasTm, tm;
    want("white_balance", hasWb, wb);
    want("grayscale", hasGs, gs);
    want("desktop_gamma", hasDg, dg);
    want("tonemap", hasTm, tm);
    if (!isHDR && (hasDg || hasTm)) {
        // Both are HDR-only layers; asking to change them in SDR is a client error, asking
        // for them OFF is a harmless no-op (a "disable everything" client).
        if ((hasDg && dg) || (hasTm && tm)) { error = "desktop_gamma / tonemap are HDR-only layers"; return; }
        hasDg = hasTm = false;
    }
    JsonValue before = JObj(), after = JObj();
    bool mhcChanged = false, tmChanged = false, dgChanged = false, mhcEnabled = false, profileNamed = false;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MonitorSettings& ms = g_gui.monitorSettings[mon];
        MHCSettings& m = isHDR ? ms.hdrMHC : ms.sdrMHC;
        LayersJson(ms, isHDR, before);
        if (hasWb && m.whiteBalanceEnabled != wb) { m.whiteBalanceEnabled = wb; mhcChanged = true; }
        if (hasGs && m.correctionGrayscale.enabled != gs) {
            m.correctionGrayscale.enabled = gs;
            if (gs && m.correctionGrayscale.points.empty()) {
                if (isHDR) m.correctionGrayscale.initLinearPQ(); else m.correctionGrayscale.initLinear();
            }
            mhcChanged = true;
        }
        if (hasDg && m.desktopGammaEnabled != dg) { m.desktopGammaEnabled = dg; mhcChanged = true; dgChanged = true; }
        if (hasTm && ms.hdrColorCorrection.tonemap.enabled != tm) { ms.hdrColorCorrection.tonemap.enabled = tm; tmChanged = true; }
        mhcEnabled = m.enabled;
        profileNamed = !m.profileName.empty();
    }
    bool regenerated = false;
    if (mhcChanged && profileNamed) {
        // Without g_monitorSettingsMutex held (RegenerateMhcIfActive snapshots under it).
        RegenerateMhcIfActive(mon, isHDR);
        regenerated = true;
    }
    if (dgChanged) {
        bool dgActive = dg && mhcEnabled;
        g_userDesktopGammaMode.store(dgActive);
        if (!g_gammaWhitelistActive.load()) g_desktopGammaMode.store(dgActive);
    }
    if (tmChanged) {
        if (g_gui.isRunning) {
            UpdateColorCorrectionLive(mon, true);
            if (g_dwmHookMode.load()) UpdateDwmHookSharedConfig();
            else DwmHookReevaluateOverlay();
        } else if (tm) {
            StartProcessing();
        }
    }
    if (mhcChanged || tmChanged) {
        UpdateMhcFlagsLive(mon);
        SaveSettings();
        UpdateGUIState();
        UpdateColorCorrectionControls();   // the GUI checkboxes follow the pipe
    }
    std::string profileName;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        const MonitorSettings& ms = g_gui.monitorSettings[mon];
        LayersJson(ms, isHDR, after);
        profileName = WideToUtf8((isHDR ? ms.hdrMHC : ms.sdrMHC).profileName);
    }
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("before", before);
    result.set("after", after);
    result.set("regenerated", JBool(regenerated));
    result.set("profile_name", JStr(profileName));
}

void DoDisableAll(const JsonValue& /*p*/, JsonValue& result, std::string& /*error*/) {
    CleanupActiveGsLive();  // revert any in-flight grayscale live preview first
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        for (auto& s : g_gui.monitorSettings) {
            s.sdrPath.clear();
            s.hdrPath.clear();
            s.sdrColorCorrection.primariesEnabled = false;
            s.sdrColorCorrection.grayscale.enabled = false;
            s.hdrColorCorrection.primariesEnabled = false;
            s.hdrColorCorrection.grayscale.enabled = false;
            s.hdrColorCorrection.tonemap.enabled = false;
        }
    }
    SaveSettings();
    ReapplyProcessing();
    result.set("corrections_enabled", JBool(false));
}

void DoMhcSetPrimaries(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    const JsonValue* prim = p.find("primaries");
    if (!prim || prim->type != JsonValue::Obj) { error = "missing parameter: primaries"; return; }
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MHCSettings& m = isHDR ? g_gui.monitorSettings[mon].hdrMHC : g_gui.monitorSettings[mon].sdrMHC;
        // Measured DISPLAY primaries; the MHC matrix maps the standard source to these.
        m.customPrimaries.Rx = (float)prim->getNum("rx", m.customPrimaries.Rx);
        m.customPrimaries.Ry = (float)prim->getNum("ry", m.customPrimaries.Ry);
        m.customPrimaries.Gx = (float)prim->getNum("gx", m.customPrimaries.Gx);
        m.customPrimaries.Gy = (float)prim->getNum("gy", m.customPrimaries.Gy);
        m.customPrimaries.Bx = (float)prim->getNum("bx", m.customPrimaries.Bx);
        m.customPrimaries.By = (float)prim->getNum("by", m.customPrimaries.By);
        m.primariesEnabled = true;
    }
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("mhc", JObj());
}

void DoMhcSetWhite(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    double x = p.getNum("x", 0.3127);
    double y = p.getNum("y", 0.3290);
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MHCSettings& m = isHDR ? g_gui.monitorSettings[mon].hdrMHC : g_gui.monitorSettings[mon].sdrMHC;
        m.customPrimaries.Wx = (float)x;
        m.customPrimaries.Wy = (float)y;
    }
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("mhc", JObj());
}

void DoMhcSetGrayscale(const JsonValue& p, JsonValue& result, std::string& error, bool correction) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MHCSettings& m = isHDR ? g_gui.monitorSettings[mon].hdrMHC : g_gui.monitorSettings[mon].sdrMHC;
        ApplyGrayscalePayload(correction ? m.correctionGrayscale : m.baseGrayscale, p);
    }
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("mhc", JObj());
}

// Import a full-resolution 1D .cube as the MHC base grayscale/EOTF correction — the
// path ColourSpace/DisplayCal use via the GUI file-import, now reachable over the pipe
// so DLC can carry a dense per-channel TRC instead of the coarse 32-point editable table
// (kMaxMhcGrayscalePoints), which is far too sparse for a PQ EOTF. Sets sourceFilePath/
// sourceIs1DCube so mhc.apply's BuildMHC2Params loads the cube (Load1DCubeLUT ->
// params.corrR/G/B) and bakes the 4096-entry (HDR) / 1024-entry (SDR) MHC2 LUT directly.
// The matrix is untouched: the cube carries ONLY per-channel tone; set_primaries/set_white
// still own primaries + white. peak_nits feeds the HDR MHC2 luminance metadata (MaxCLL).
void DoMhcSetBaseLut(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    std::wstring cube = Utf8ToWide(p.getStr("cube_path"));
    if (cube.empty()) { error = "missing parameter: cube_path"; return; }
    if (GetFileAttributesW(cube.c_str()) == INVALID_FILE_ATTRIBUTES) {
        error = "cube_path does not exist";
        return;
    }
    // Validate up-front so a malformed cube fails here with a clear error, rather than
    // silently falling back to identity at apply time.
    std::vector<float> r, g, b;
    if (!Load1DCubeLUT(cube, r, g, b)) {
        error = "cube_path is not a valid 1D .cube LUT";
        return;
    }
    double peakNits = p.getNum("peak_nits", 0.0);
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MHCSettings& m = isHDR ? g_gui.monitorSettings[mon].hdrMHC : g_gui.monitorSettings[mon].sdrMHC;
        m.sourceFilePath = cube;
        m.sourceIs1DCube = true;
        m.hasPerChannelTRC = false;
        m.baseGrayscale.enabled = true;            // base grayscale now comes from the cube
        if (peakNits > 0.0) m.baseGrayscale.peakNits = (float)peakNits;
    }
    JsonValue mo = JObj();
    mo.set("source_is_1d_cube", JBool(true));
    mo.set("lut_size", JNum((int)r.size()));
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("mhc", mo);
}

void DoMhcApply(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MHCSettings& m = isHDR ? g_gui.monitorSettings[mon].hdrMHC : g_gui.monitorSettings[mon].sdrMHC;
        m.enabled = true;
    }
    // GenerateAndInstallMhcProfile snapshots settings under the mutex internally,
    // so it MUST be called without g_monitorSettingsMutex held.
    if (!GenerateAndInstallMhcProfile(mon, isHDR)) {
        error = "GenerateAndInstallMhcProfile failed";
        return;
    }
    UpdateMhcFlagsLive(mon);
    SaveSettings();
    JsonValue m = JObj();
    m.set("applied", JBool(true));
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        const MHCSettings& s = isHDR ? g_gui.monitorSettings[mon].hdrMHC : g_gui.monitorSettings[mon].sdrMHC;
        m.set("profile_name", JStr(WideToUtf8(s.profileName)));
    }
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("mhc", m);
}

void DoMhcRemove(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    DisplayInfo di;
    bool haveDi = GetDisplayInfoForMonitor(mon, di);
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MHCSettings& m = isHDR ? g_gui.monitorSettings[mon].hdrMHC : g_gui.monitorSettings[mon].sdrMHC;
        if (haveDi && !m.profileName.empty())
            RemoveMHC2Profile(m.profileName, di.adapterId, di.sourceId, isHDR);
        m.enabled = false;
    }
    UpdateMhcFlagsLive(mon);
    SaveSettings();
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("removed", JBool(true));
}

void DoVerifyMhc(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    bool verified;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        const MHCSettings& m = isHDR ? g_gui.monitorSettings[mon].hdrMHC : g_gui.monitorSettings[mon].sdrMHC;
        verified = m.enabled && !m.profileName.empty();
    }
    result.set("verified", JBool(verified));
}

void DoSet3dlut(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    std::wstring cube = Utf8ToWide(p.getStr("cube_path"));
    if (cube.empty()) { error = "missing parameter: cube_path"; return; }
    if (GetFileAttributesW(cube.c_str()) == INVALID_FILE_ATTRIBUTES) {
        error = "cube_path does not exist";
        return;
    }
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MonitorSettings& ms = g_gui.monitorSettings[mon];
        if (isHDR) ms.hdrPath = cube; else ms.sdrPath = cube;
    }
    SaveSettings();
    ReapplyProcessing();
    // Reflect the pipe-applied path in the GUI's LUT box so an operator sees it without a
    // restart. Safe to touch the controls directly: mutating methods run on the GUI thread
    // (dispatched via WM_CALIB_CMD). Only refresh when the affected monitor is the one
    // currently shown, otherwise we'd overwrite another monitor's box.
    if (mon == g_gui.currentMonitor) {
        SetPathText(isHDR ? g_gui.hwndHdrPath : g_gui.hwndSdrPath, cube.c_str());
    }
    JsonValue rt = JObj();
    rt.set("cube_path", JStr(WideToUtf8(cube)));
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("runtime", rt);
}

void DoClear3dlut(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MonitorSettings& ms = g_gui.monitorSettings[mon];
        if (isHDR) ms.hdrPath.clear(); else ms.sdrPath.clear();
    }
    SaveSettings();
    ReapplyProcessing();
    if (mon == g_gui.currentMonitor) {
        SetPathText(isHDR ? g_gui.hwndHdrPath : g_gui.hwndSdrPath, L"");
    }
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("runtime", JObj());
}

// Runtime (shader) grayscale tweak = DesktopLUT's main-GUI grayscale-correction
// path (ColorCorrectionSettings.grayscale). This is the fast PROXY tier for the
// GS+WB final tweak: DLC iterates it without an ICC re-bake, then bakes the
// converged values into the editable MHC grayscale/WB controls. Payload mirrors
// the MHC grayscale shape (point_count / points / deviations{r,g,b}), wrapped in
// a "grayscale_tweak" object. The per-channel deviations carry both grayscale
// tracking (their shape) and white balance (their DC component).
void DoSetGrayscaleTweak(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    const JsonValue* tweak = p.find("grayscale_tweak");
    if (!tweak || tweak->type != JsonValue::Obj) { error = "missing parameter: grayscale_tweak"; return; }
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        ColorCorrectionSettings& cc = isHDR ? g_gui.monitorSettings[mon].hdrColorCorrection
                                            : g_gui.monitorSettings[mon].sdrColorCorrection;
        ApplyGrayscalePayload(cc.grayscale, *tweak);  // sets enabled = true
    }
    SaveSettings();
    ReapplyProcessing();
    JsonValue rt = JObj();
    rt.set("grayscale_tweak", JBool(true));  // enabled
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("runtime", rt);
}

void DoDisableGrayscaleTweak(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        ColorCorrectionSettings& cc = isHDR ? g_gui.monitorSettings[mon].hdrColorCorrection
                                            : g_gui.monitorSettings[mon].sdrColorCorrection;
        cc.grayscale.enabled = false;
    }
    SaveSettings();
    ReapplyProcessing();
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("runtime", JObj());
}

// --- Correction-grayscale live preview handlers (see GsLiveState above) -------

// mhc.grayscale_live_begin {monitor, mode}: engage the live-edit preview. Spins up
// the overlay if needed, strips PERM_GS from the active MHC permutation so the shader
// can preview correctionGrayscale on top of the base calibration, and flips
// corrGsPreviewActive so the shader grayscale passes through MHC suppression
// (render.cpp:346). Caches savedPerm + the pre-begin correctionGrayscale for revert.
void DoGrayscaleLiveBegin(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    // Don't double-drive the preview if a human has the editor dialog open.
    if (g_mhcEditDialogOpen.load()) { error = "grayscale editor already open in the GUI"; return; }
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        auto it = g_gsLive.find({mon, isHDR});
        if (it != g_gsLive.end() && it->second.active) {
            error = "grayscale live preview already active for this monitor/mode";
            return;
        }
    }

    bool livePreview = false, startedForPreview = false, startedOverlayForPreview = false;
    EnsureProcessingForPreview(mon, isHDR, livePreview, startedForPreview, startedOverlayForPreview);
    if (!livePreview) {
        error = "overlay not available for live preview (monitor mode does not match run mode, or processing could not start)";
        return;
    }
    // Suppress MHC profile monitoring for the duration of the edit (EnsureProcessingForPreview
    // already set it when it spun up the overlay itself).
    if (!startedOverlayForPreview) g_mhcEditDialogOpen.store(true);

    GsLiveState st;
    st.active = true;
    st.startedForPreview = startedForPreview;
    st.startedOverlayForPreview = startedOverlayForPreview;
    bool hadProfile = false;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MHCSettings& m = isHDR ? g_gui.monitorSettings[mon].hdrMHC : g_gui.monitorSettings[mon].sdrMHC;
        st.savedPerm = m.activePerm;
        st.savedCorrectionGs = m.correctionGrayscale;  // for cancel/abort restore
        hadProfile = m.enabled && !m.profileName.empty();
    }

    // Engage the live preview. SDR (realization A; CODEX_PREVIEW_BAKE_PROMPT.md): neutralize
    // scanout to IDENTITY (transient passthrough profile) and have the shader reproduce the WHOLE
    // MHC2 transform + the live correction, so the preview is bit-identical to the bake (incl.
    // per-channel / white balance). HDR keeps the legacy path (strip PERM_GS; full-preview is
    // out of scope for HDR). Falls back to the legacy strip if scanout reproduction is unavailable.
    bool fullPreview = false;
    float previewResult9[9] = { 1,0,0, 0,1,0, 0,0,1 };
    std::vector<float> previewBaseLut[3];
    if (!isHDR && hadProfile) {
        uint8_t strippedPerm = (uint8_t)(st.savedPerm & ~MHCSettings::PERM_GS);
        if (ComputeSdrPreviewScanout(mon, strippedPerm, previewResult9,
                                     previewBaseLut[0], previewBaseLut[1], previewBaseLut[2])) {
            st.sdrPassthroughName = EngageSdrPassthroughScanout(mon);
            fullPreview = !st.sdrPassthroughName.empty();
        }
    }
    if (!fullPreview && hadProfile && (st.savedPerm & MHCSettings::PERM_GS))
        SwapMhcToPermutation(mon, isHDR, (uint8_t)(st.savedPerm & ~MHCSettings::PERM_GS));

    {
        std::lock_guard<std::mutex> lk(g_monitorsMutex);
        for (auto& ctx : g_monitors)
            if (ctx.index == mon) {
                ctx.corrGsPreviewActive = true;
                if (fullPreview) {
                    memcpy(ctx.previewResult, previewResult9, sizeof(previewResult9));
                    for (int ch = 0; ch < 3; ch++) ctx.previewBaseLut[ch] = std::move(previewBaseLut[ch]);
                    ctx.previewBaseLutSize = 1024;
                    ctx.previewBaseLutDirty = true;
                    ctx.corrGsFullPreviewActive = true;
                }
                ctx.cbDirty = true;
                break;
            }
    }
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        g_gsLive[{mon, isHDR}] = st;
    }
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("preview", JBool(true));
}

// mhc.grayscale_set_live {monitor, mode, grayscale:{point_count,points,deviations}}:
// the per-patch nudge. Applies the payload to correctionGrayscale and pushes it to the
// overlay so the next frame reflects it. NOTE: UpdateColorCorrectionLive reads
// sdr/hdrColorCorrection (the shader CC), NOT correctionGrayscale, so it CANNOT be used
// here — we replicate the GUI editor's live-preview callback (gui.cpp ID_MHC_*_GS_EDIT)
// which pushes a temp CC carrying correctionGrayscale straight onto the pending queue.
void DoGrayscaleSetLive(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    const JsonValue* gs = p.find("grayscale");
    if (!gs || gs->type != JsonValue::Obj) { error = "missing parameter: grayscale"; return; }

    ColorCorrectionSettings tempCC;  // default ctor: only grayscale is enabled below
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        auto it = g_gsLive.find({mon, isHDR});
        if (it == g_gsLive.end() || !it->second.active) {
            error = "no active grayscale live preview (call mhc.grayscale_live_begin first)";
            return;
        }
        MHCSettings& m = isHDR ? g_gui.monitorSettings[mon].hdrMHC : g_gui.monitorSettings[mon].sdrMHC;
        ApplyGrayscalePayload(m.correctionGrayscale, *gs);  // sets enabled = true
        tempCC.grayscale = m.correctionGrayscale;           // snapshot for the overlay push
    }
    ColorCorrectionData data = ConvertColorCorrection(tempCC, isHDR);
    {
        std::lock_guard<std::mutex> lk(g_colorCorrectionMutex);
        g_pendingColorCorrections.erase(
            std::remove_if(g_pendingColorCorrections.begin(), g_pendingColorCorrections.end(),
                [mon, isHDR](const PendingColorCorrection& pc) {
                    return pc.monitorIndex == mon && pc.isHDR == isHDR;
                }),
            g_pendingColorCorrections.end());
        g_pendingColorCorrections.push_back({ mon, isHDR, data, false, false });
        g_hasPendingColorCorrections.store(true, std::memory_order_release);
        if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
    }
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
}

// mhc.grayscale_commit {monitor, mode}: the editor's "OK" — bake correctionGrayscale
// into the ICC, leave it toggled on, tear down the preview. Tolerates a commit with no
// matching begin (no-op).
void DoGrayscaleCommit(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    GsLiveState st; bool found = false;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        auto it = g_gsLive.find({mon, isHDR});
        if (it != g_gsLive.end()) { st = it->second; g_gsLive.erase(it); found = true; }
    }
    if (found) FinishGsLive(mon, isHDR, st, /*bake=*/true);
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("baked", JBool(found));
}

// mhc.grayscale_cancel {monitor, mode}: abort without baking — restore the pre-begin
// correctionGrayscale and regenerate to the vanilla core ICM, tear down the preview.
// Tolerates a cancel with no matching begin (no-op).
void DoGrayscaleCancel(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    GsLiveState st; bool found = false;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        auto it = g_gsLive.find({mon, isHDR});
        if (it != g_gsLive.end()) { st = it->second; g_gsLive.erase(it); found = true; }
    }
    if (found) FinishGsLive(mon, isHDR, st, /*bake=*/false);
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("canceled", JBool(found));
}

bool IsMutatingMethod(const std::string& m) {
    return m == "calibration.enter" || m == "calibration.exit" ||
           m == "corrections.disable_all" || m == "layers.set" || m.rfind("mhc.", 0) == 0 ||
           m.rfind("runtime.", 0) == 0;  // set_3dlut / clear_3dlut / *_grayscale_tweak
}

// ===========================================================================
// Dispatch
// ===========================================================================
std::string Dispatch(const std::string& request) {
    JsonValue root;
    try {
        JsonParser parser(request);
        root = parser.parse();
    } catch (const std::exception& e) {
        return ErrResponse(std::string("invalid JSON: ") + e.what());
    } catch (...) {
        return ErrResponse("invalid JSON request");
    }
    if (root.type != JsonValue::Obj) return ErrResponse("request must be a JSON object");

    std::string method = root.getStr("method");
    if (method.empty()) return ErrResponse("missing method");
    const JsonValue* paramsPtr = root.find("params");
    JsonValue emptyParams = JObj();
    const JsonValue& params = (paramsPtr && paramsPtr->type == JsonValue::Obj) ? *paramsPtr : emptyParams;

    JsonValue result = JObj();
    std::string error;

    try {
        if (method == "state.get") {
            HandleStateGet(result);
        } else if (method == "calibration.status") {
            HandleCalibStatus(result);
        } else if (method == "windows.query_profiles") {
            HandleQueryProfiles(params, result);
        } else if (method == "windows.query_gamma_ramp") {
            HandleQueryGammaRamp(params, result);
        } else if (method == "windows.query_monitors") {
            HandleQueryMonitors(params, result);
        } else if (method == "windows.set_hdr") {
            HandleSetHdr(params, result, error);  // off-thread DisplayConfig flip
        } else if (method == "maintenance.verify_mhc") {
            DoVerifyMhc(params, result, error);  // read-only, safe off the GUI thread
        } else if (IsMutatingMethod(method)) {
            if (!g_gui.hwndMain) {
                error = "GUI window not available";
            } else {
                CalibGuiRequest req{&method, &params, &result, &error};
                DWORD_PTR res = 0;
                LRESULT ok = SendMessageTimeoutW(g_gui.hwndMain, WM_CALIB_CMD, (WPARAM)&req, 0,
                                                 SMTO_NORMAL, kGuiTimeoutMs, &res);
                if (!ok && error.empty()) error = "GUI thread did not respond";
            }
        } else {
            error = "unknown method: " + method;
        }
    } catch (const std::exception& e) {
        error = std::string("exception: ") + e.what();
    } catch (...) {
        error = "unhandled exception";
    }

    return error.empty() ? OkResponse(result) : ErrResponse(error);
}

// ===========================================================================
// Pipe server
// ===========================================================================
std::atomic<bool> g_stop{false};
HANDLE g_serverThread = nullptr;

bool ServerEnabled() {
    // In-app toggle (Settings checkbox / tray) — the human's deliberate arming
    // action. Persisted, so a checkbox left on re-arms at next launch.
    if (g_calibrationControlEnabled.load()) return true;
    // Headless/dev/CI enable: env var or a flag file next to the exe.
    wchar_t env[8] = {0};
    DWORD n = GetEnvironmentVariableW(L"DESKTOPLUT_CALIBRATION", env, 8);
    if (n > 0 && env[0] != L'0') return true;
    wchar_t path[MAX_PATH];
    if (GetModuleFileNameW(nullptr, path, MAX_PATH) == 0) return false;
    std::wstring p(path);
    size_t slash = p.find_last_of(L"\\/");
    std::wstring dir = (slash != std::wstring::npos) ? p.substr(0, slash + 1) : L"";
    std::wstring flag = dir + L"DesktopLUT_Calibration.flag";
    return GetFileAttributesW(flag.c_str()) != INVALID_FILE_ATTRIBUTES;
}

// Build a protected DACL granting access to the current user + SYSTEM only.
PSECURITY_DESCRIPTOR BuildLocalUserSd() {
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return nullptr;
    DWORD len = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &len);
    std::vector<BYTE> buf(len ? len : 1);
    LPWSTR sidStr = nullptr;
    if (len && GetTokenInformation(token, TokenUser, buf.data(), len, &len)) {
        PTOKEN_USER tu = (PTOKEN_USER)buf.data();
        ConvertSidToStringSidW(tu->User.Sid, &sidStr);
    }
    CloseHandle(token);
    if (!sidStr) return nullptr;
    std::wstring sddl = L"D:P(A;;GA;;;" + std::wstring(sidStr) + L")(A;;GA;;;SY)";
    LocalFree(sidStr);
    PSECURITY_DESCRIPTOR sd = nullptr;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1, &sd, nullptr))
        return nullptr;
    return sd;
}

void HandleConnection(HANDLE pipe) {
    std::string request;
    char buf[4096];
    DWORD read = 0;
    while (request.size() < kMaxRequestBytes) {
        if (!ReadFile(pipe, buf, sizeof(buf), &read, nullptr) || read == 0) break;
        request.append(buf, read);
        size_t nl = request.find('\n');
        if (nl != std::string::npos) { request.resize(nl); break; }
    }
    if (request.size() >= kMaxRequestBytes) {
        std::string resp = ErrResponse("request too large") + "\n";
        DWORD written = 0;
        WriteFile(pipe, resp.data(), (DWORD)resp.size(), &written, nullptr);
        return;
    }
    std::string response = Dispatch(request) + "\n";
    DWORD written = 0;
    WriteFile(pipe, response.data(), (DWORD)response.size(), &written, nullptr);
}

DWORD WINAPI ServerThreadProc(LPVOID) {
    PSECURITY_DESCRIPTOR sd = BuildLocalUserSd();
    SECURITY_ATTRIBUTES sa{sizeof(sa), sd, FALSE};
    while (!g_stop.load()) {
        HANDLE pipe = CreateNamedPipeW(
            kPipeName,
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,                       // single instance — one client at a time
            64 * 1024, 64 * 1024,
            0,
            sd ? &sa : nullptr);
        if (pipe == INVALID_HANDLE_VALUE) { Sleep(500); continue; }

        BOOL connected = ConnectNamedPipe(pipe, nullptr)
                             ? TRUE
                             : (GetLastError() == ERROR_PIPE_CONNECTED);
        if (g_stop.load()) { CloseHandle(pipe); break; }
        if (connected) {
            HandleConnection(pipe);
            FlushFileBuffers(pipe);
        }
        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);
    }
    if (sd) LocalFree(sd);
    return 0;
}

}  // namespace

// ===========================================================================
// Public entry points
// ===========================================================================
void StartCalibrationIpcServer() {
    if (g_serverThread) return;
    if (!ServerEnabled()) return;  // SECURITY: opt-in only
    g_stop.store(false);
    g_serverThread = CreateThread(nullptr, 0, ServerThreadProc, nullptr, 0, nullptr);
}

void StopCalibrationIpcServer() {
    if (!g_serverThread) return;
    g_stop.store(true);
    // Unblock a pending ConnectNamedPipe by connecting to ourselves.
    HANDLE h = CreateFileW(kPipeName, GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, 0, nullptr);
    if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
    WaitForSingleObject(g_serverThread, 5000);
    CloseHandle(g_serverThread);
    g_serverThread = nullptr;
}

LRESULT HandleCalibrationGuiCommand(WPARAM wParam, LPARAM /*lParam*/) {
    CalibGuiRequest* r = reinterpret_cast<CalibGuiRequest*>(wParam);
    if (!r || !r->method || !r->params || !r->result || !r->error) return 0;
    try {
        const std::string& m = *r->method;
        if (m == "calibration.enter") DoEnterNeutral(*r->params, *r->result, *r->error);
        else if (m == "calibration.exit") DoExitCalibration(*r->params, *r->result, *r->error);
        else if (m == "corrections.disable_all") DoDisableAll(*r->params, *r->result, *r->error);
        else if (m == "layers.set") DoLayersSet(*r->params, *r->result, *r->error);
        else if (m == "mhc.set_primaries") DoMhcSetPrimaries(*r->params, *r->result, *r->error);
        else if (m == "mhc.set_white") DoMhcSetWhite(*r->params, *r->result, *r->error);
        else if (m == "mhc.set_base_grayscale") DoMhcSetGrayscale(*r->params, *r->result, *r->error, false);
        else if (m == "mhc.set_base_lut") DoMhcSetBaseLut(*r->params, *r->result, *r->error);
        else if (m == "mhc.set_correction_grayscale") DoMhcSetGrayscale(*r->params, *r->result, *r->error, true);
        else if (m == "mhc.grayscale_live_begin") DoGrayscaleLiveBegin(*r->params, *r->result, *r->error);
        else if (m == "mhc.grayscale_set_live") DoGrayscaleSetLive(*r->params, *r->result, *r->error);
        else if (m == "mhc.grayscale_commit") DoGrayscaleCommit(*r->params, *r->result, *r->error);
        else if (m == "mhc.grayscale_cancel") DoGrayscaleCancel(*r->params, *r->result, *r->error);
        else if (m == "mhc.apply") DoMhcApply(*r->params, *r->result, *r->error);
        else if (m == "mhc.remove") DoMhcRemove(*r->params, *r->result, *r->error);
        else if (m == "maintenance.verify_mhc") DoVerifyMhc(*r->params, *r->result, *r->error);
        else if (m == "runtime.set_3dlut") DoSet3dlut(*r->params, *r->result, *r->error);
        else if (m == "runtime.clear_3dlut") DoClear3dlut(*r->params, *r->result, *r->error);
        else if (m == "runtime.set_grayscale_tweak") DoSetGrayscaleTweak(*r->params, *r->result, *r->error);
        else if (m == "runtime.disable_grayscale_tweak") DoDisableGrayscaleTweak(*r->params, *r->result, *r->error);
        else *r->error = "unknown method: " + m;
    } catch (const std::exception& e) {
        *r->error = std::string("exception: ") + e.what();
    } catch (...) {
        *r->error = "unhandled exception";
    }
    return 0;
}
