// DesktopLUT - desktoplut_ipc_server.cpp
// See desktoplut_ipc_server.h for the security model.

#include "desktoplut_ipc_server.h"

#include <windows.h>
#include <sddl.h>

#include <atomic>
#include <algorithm>
#include <climits>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "types.h"
#include "globals.h"
#include "calib_snapshot.h"
#include "gui_mhc.h"
#include "gui_shared.h"
#include "mhc.h"
#include "fald.h"
#include "displayconfig.h"
#include "ipc_json.h"
#include "ipc_grayscale.h"
#include "ipc_client_check.h"
#include "settings.h"
#include "processing.h"
#include "gui.h"
#include "dwm_inject.h"

#pragma comment(lib, "Advapi32.lib")

namespace {

const wchar_t* kPipeName = L"\\\\.\\pipe\\DesktopLUT.Calibration";
constexpr size_t kMaxRequestBytes = 256 * 1024;  // DoS guard
constexpr DWORD kGuiTimeoutMs = 60000;           // MHC install can be slow
// The calibration wire-contract version this server speaks, reported in state.get (fable
// audit Phase 9, T1). DLC checks it at preflight (desktoplut_client.CONTRACT_VERSION) so a
// mismatch reads "update DLC/DesktopLUT" instead of "unknown method" mid-run. A client that
// sees no field at all is talking to a pre-versioning build and assumes 1, so this must stay
// in lockstep with DLC's constant. Bump ONLY for a change a tolerant client cannot absorb;
// additive fields never require a bump.
constexpr int kCalibrationContractVersion = 1;

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
// JSON (src/ipc_json.h: bounded depth / value count, strict numbers)
// ===========================================================================
using namespace ipc_json;

// Luminance for the wire: Windows' SDR white steps are 0.08 nit, so 2 decimals are exact (no float tail).
static double RoundNits(float v) { return std::round((double)v * 100.0) / 100.0; }

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
    // Pre-session settings per DISPLAY (identity-keyed, with the set of modes entered). The
    // first capture of a display wins for the whole session; only calibration.exit drops them.
    // See calib_snapshot.h (fable audit Phase 9 T2 + the 2026-09-27 stale-snapshot bug).
    CalibSnapshotStore snapshots;
};
std::mutex g_calibMutex;
CalibState g_calib;

// Marshaling envelope: pipe thread -> GUI thread. Heap-owned and shared, looked up by id:
// the pipe thread waits at most kGuiTimeoutMs, but a handler already running on the GUI
// thread keeps running after that and must write into memory it co-owns — never into the
// pipe thread's (by then unwound) stack. WM_CALIB_CMD carries only the id, so a stale or
// forged message finds nothing to run. The message is POSTED and the pipe thread waits on
// {doneEvent, g_stopEvent}: a disarm (StopCalibrationIpcServer, on the GUI thread) then ends the
// wait at once and un-registers the call, so the queued message runs nothing — a SendMessage
// could not be abandoned that way and would execute the command after the human disarmed.
struct CalibGuiCall {
    std::string method;
    JsonValue params;
    JsonValue result = JObj();
    std::string error;
    std::atomic<bool> done{false};   // release-stored by the GUI thread after result/error
    HANDLE doneEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);   // set after `done`
    CalibGuiCall() = default;
    CalibGuiCall(const CalibGuiCall&) = delete;
    CalibGuiCall& operator=(const CalibGuiCall&) = delete;
    ~CalibGuiCall() { if (doneEvent) CloseHandle(doneEvent); }
};
// Set by StopCalibrationIpcServer: ends every pipe wait and every pending GUI-call wait.
// Manual reset; process lifetime (never closed — the server thread may outlive a Stop).
HANDLE g_stopEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
// The connection whose request the server thread is dispatching (null elsewhere, e.g. in tests that
// call Dispatch directly): mutating verbs log the client's PID and image path (T2.2).
thread_local HANDLE t_clientPipe = nullptr;
std::mutex g_guiCallsMutex;
std::map<uint64_t, std::shared_ptr<CalibGuiCall>> g_guiCalls;   // under g_guiCallsMutex
uint64_t g_nextGuiCallId = 1;                                    // under g_guiCallsMutex

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
    // Desktop gamma waited for the live edit: catch up with the SDR white level (debounced re-check).
    if (g_gui.hwndMain) SetTimer(g_gui.hwndMain, SDR_WHITE_CHECK_TIMER_ID, SDR_WHITE_CHECK_DEBOUNCE_MS, nullptr);
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
            s.hdrColorCorrection.tonemap.enabled || s.hdrColorCorrection.fald.enabled ||
            s.sdrColorCorrection.fald.enabled)
            return true;
    }
    return false;
}

// Restart the overlay/hook processing so cleared/loaded LUTs and shader flags
// take effect, mirroring what the GUI's Apply path does.
void ReapplyProcessing() {
    StopProcessing();
    FaldTrace("ReapplyProcessing: AnyCorrectionActive?");
    if (AnyCorrectionActive()) { FaldTrace("ReapplyProcessing: StartProcessing"); StartProcessing(); }
    FaldTrace("ReapplyProcessing: end");
}

// Validates a grayscale payload and, only if it is entirely valid, stores it into `gs` (point count,
// points, per-channel gains, enabled = true). Rules: src/ipc_grayscale.h. False + error otherwise,
// with `gs` untouched — a malformed payload used to bake a square-root curve and report ok:true.
bool ApplyGrayscalePayload(GrayscaleSettings& gs, const JsonValue& p, bool isHDR, std::string& error) {
    return ipc_grayscale::GrayscaleFromPayload(p, isHDR, gs.pointCount, gs, error);
}

// ===========================================================================
// DWM hook twin-panel routing (25H2 first-present order-match)
// ===========================================================================
// WIRE CONTRACT (consumed by DLC's hook-routing self-check). On 25H2 the hook DLL cannot
// read a monitor position from a DWM overlay context; two identical panels are assigned by
// first-present ORDER, re-rolled on every injection (2026-09-03: a whole 3dlut-only run
// measured the wrong panel). The DLL now persists its assignment per dwm.exe lifetime; this
// reports it and lets a client swap / confirm / clear it.
//   hook: { active, needs_check, routing?: { session, stale, confirmed, entries: [
//           { ctx, left, top, method: unique|bpc|scan|pinned|order|legacy|provisional|replaced|beacon,
//             monitor|null } ] } }
// `beacon` = identified positively by the host's identity beacon (a colour the DLL read from
// the context's back-buffer corner): unambiguous, so it never sets needs_check. The host runs a
// beacon session after every injection; hook.set_routing {action:"identify"} runs one on demand.
// needs_check = an entry was assigned by order (or is a pin of one), or replaced a context DWM
// destroyed (`replaced`: inferred from which twin kept presenting), and no client confirmed it
// through a meter; or an entry is still `provisional` (a replacement guess the DLL has not yet
// settled — always unverified); or the recorded dwm.exe is gone. No routing file yet => no
// `routing` key (the client treats that as unknown).
JsonValue BuildHookStateJson() {
    JsonValue hook = JObj();
    hook.set("active", JBool(IsDwmHookActive()));
    DwmHookRouting r = ReadDwmHookRouting();
    bool needsCheck = false;
    if (r.present) {
        JsonValue routing = JObj();
        routing.set("session", JStr(r.session));
        routing.set("stale", JBool(r.stale));
        routing.set("confirmed", JBool(r.confirmed));
        // Monitor index by desktop origin — the same lookup UpdateDwmHookSharedConfig uses.
        std::vector<std::pair<int, int>> origins;
        {
            std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
            for (size_t mi = 0; mi < g_gui.monitors.size(); ++mi) {
                MONITORINFO info = { sizeof(info) };
                if (GetMonitorInfo(g_gui.monitors[mi], &info))
                    origins.emplace_back((int)info.rcMonitor.left, (int)info.rcMonitor.top);
                else
                    origins.emplace_back(INT_MIN, INT_MIN);
            }
        }
        JsonValue entries = JArr();
        for (const auto& e : r.entries) {
            JsonValue j = JObj();
            j.set("ctx", JStr("0x" + e.ctx));
            j.set("left", JNum(e.left));
            j.set("top", JNum(e.top));
            j.set("method", JStr(e.method));
            int idx = -1;
            for (size_t i = 0; i < origins.size(); ++i)
                if (origins[i].first == e.left && origins[i].second == e.top) { idx = (int)i; break; }
            j.set("monitor", idx >= 0 ? JNum(idx) : JsonValue());
            entries.arr.push_back(j);
            if ((e.method == "order" || e.method == "pinned" || e.method == "replaced") && !r.confirmed) needsCheck = true;
            if (e.method == "provisional") needsCheck = true;
        }
        if (r.stale) needsCheck = true;
        routing.set("entries", entries);
        hook.set("routing", routing);
    }
    hook.set("needs_check", JBool(needsCheck));
    return hook;
}

// ===========================================================================
// Read-only handlers (served on the pipe thread)
// ===========================================================================
// The correction grayscale as DLC reads it back (fable audit Phase 9, T3), in the SAME
// decomposition ApplyGrayscalePayload stores: `points` already carry the luminance (main-slider)
// scale and `deviations` the per-channel BALANCE. So the block handed back VERBATIM to
// mhc.set_correction_grayscale (no luminance / rgb keys) reproduces the curve exactly — never
// re-derive it through a signal-domain bridge, which would treat those points as the x-grid.
// `enabled` is reported for honesty (it is the same bool as layers[key].grayscale); a client
// restores it with layers.set {grayscale}, since ApplyGrayscalePayload forces it true.
JsonValue GrayscaleJson(const GrayscaleSettings& gs) {
    JsonValue out = JObj();
    out.set("enabled", JBool(gs.enabled));
    out.set("point_count", JNum((double)gs.pointCount));
    JsonValue pts = JArr();
    for (float v : gs.points) pts.arr.push_back(JNum((double)v));
    out.set("points", std::move(pts));
    static const char* kChannels[3] = { "r", "g", "b" };
    JsonValue devs = JObj();
    for (int c = 0; c < 3; ++c) {
        JsonValue chan = JArr();
        for (float v : gs.rgbDeviations[c]) chan.arr.push_back(JNum((double)v));
        devs.set(kChannels[c], std::move(chan));
    }
    out.set("deviations", std::move(devs));
    return out;
}

void HandleStateGet(JsonValue& result) {
    result.set("contract_version", JNum((double)kCalibrationContractVersion));
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
                    // The user's CORRECTION grayscale, so DLC can snapshot it before a grayscale
                    // touch-up and put THEIR curve back on revert (Design B) instead of clearing to
                    // identity (fable audit Phase 9, T3 / F9-10). Always emitted with the entry. The
                    // settings loader fills an identity curve (initLinear / initLinearPQ), so points
                    // are empty only for a display whose settings were never loaded or edited; a
                    // client that sees NO field is talking to a build that predates it.
                    e.set("correction_grayscale", GrayscaleJson(m.correctionGrayscale));
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
                    // Desktop gamma's reference: the SDR white level (nits) recorded for this display — what
                    // any DG bake uses (it follows the live level in HDR, outside calibration sessions) ...
                    l.set("desktop_gamma_sdr_white_nits", JNum(RoundNits(m.dgSdrWhiteNits)));
                    // ... and the level the INSTALLED profile's DG was baked with; null when the active HDR
                    // profile carries no DG (DG off, swapped out, or no profile). Differs from the recorded
                    // level only while a re-bake is pending (calibration.status desktop_gamma_sdr_white_pending).
                    const float baked = (m.enabled && !m.profileName.empty() && (m.activePerm & MHCSettings::PERM_DG))
                        ? m.permDgWhiteNits[m.activePerm] : 0.0f;
                    l.set("desktop_gamma_baked_sdr_white_nits", baked > 0.0f ? JNum(RoundNits(baked)) : JsonValue());
                    l.set("tonemap_dynamic", JBool(s.hdrColorCorrection.tonemap.dynamicPeak));
                    l.set("tonemap_target_peak", JNum(s.hdrColorCorrection.tonemap.targetPeakNits));
                }
                // FALD is per mode (HDR, and SDR under ACM — 2026-09-14, work guide P7)
                const FaldSettings& fs = isHDR ? s.hdrColorCorrection.fald : s.sdrColorCorrection.fald;
                l.set("fald", JBool(fs.enabled));
                l.set("fald_params_path", JStr(WideToUtf8(fs.paramsPath)));
                l.set("fald_debug_mode", JNum((double)fs.debugMode));
                l.set("fald_ped_mode", JNum((double)fs.pedMode));
                l.set("fald_temporal_mode", JNum((double)fs.temporalMode));   // temporal drive state (runtime.fald_temporal)
                l.set("fald_tau_rise_ms", JNum((double)fs.tauRiseMs));
                l.set("fald_tau_fall_ms", JNum((double)fs.tauFallMs));
                l.set("fald_delay_frames", JNum((double)fs.delayFrames));
                l.set("fald_temporal_closure", JNum((double)fs.clockClosure));   // temporal mode 3 "panel clock" (work guide C13)
                l.set("fald_temporal_parity", JNum((double)fs.clockParity));
                // starfield balancing (runtime.fald_starfield; work guide S1)
                l.set("fald_starfield", JBool(fs.star.enabled));
                l.set("fald_star_even", JNum((double)fs.star.even));
                l.set("fald_star_lift", JNum((double)fs.star.lift));
                l.set("fald_star_target_gain", JNum((double)fs.star.targetGain));
                l.set("fald_star_target_sigma", JNum((double)fs.star.targetSigma));
                l.set("fald_star_keep_nits", JNum((double)fs.star.keepNits));
                l.set("fald_star_even_reach", JNum((double)fs.star.evenReach));
                l.set("fald_star_cap_nits", JNum((double)fs.star.capNits));
                l.set("fald_star_strength", JNum((double)fs.star.strength));
                l.set("fald_star_area_lo", JNum((double)fs.star.areaLo));
                l.set("fald_star_area_hi", JNum((double)fs.star.areaHi));
                l.set("fald_star_peak_hi", JNum((double)fs.star.peakHi));
                l.set("fald_star_reach", JNum((double)fs.star.reach));
                l.set("fald_star_nb_lo", JNum((double)fs.star.nbLo));
                l.set("fald_star_nb_hi", JNum((double)fs.star.nbHi));
                l.set("fald_ped_colour_in_file", JBool(FaldPanelFileHasPedColour(fs.paramsPath)));
                l.set("fald_boost_in_file", JBool(FaldPanelFileHasBoost(fs.paramsPath)));   // FLD4: black-frame LED boost LUT (C12)
                uint32_t transfer = 0;
                if (FaldPanelFileTransfer(fs.paramsPath, transfer))
                    l.set("fald_file_transfer", JStr(transfer == FALD_TRANSFER_GAMMA ? "gamma" : "pq"));
                layers.set(key, l);
            }
        }
    }
    result.set("mhc", mhc);
    result.set("runtime", runtime);
    result.set("layers", layers);
    result.set("hook", BuildHookStateJson());
    // Which path renders what a meter sees (DLC readiness evidence, fald-lessons item 5): the
    // awake FP16 overlay reads 0.5-2.4 % below the sleeping one at low levels; in hook mode the
    // overlay-only layers (tonemap, FALD) are off regardless of their flags.
    JsonValue overlay = JObj();
    overlay.set("awake", JBool(g_gui.isRunning && !g_overlayAutoSleep.load()));
    overlay.set("dwm_hook_mode", JBool(g_dwmHookMode.load()));
    result.set("overlay", overlay);
}

JsonValue CalibModesJson(bool sdr, bool hdr) {
    JsonValue modes = JArr();
    if (sdr) modes.arr.push_back(JStr("SDR"));
    if (hdr) modes.arr.push_back(JStr("HDR"));
    return modes;
}

// The display a capture belongs to, for a client to recognise (friendly name + EDID id), and the
// index it had when it was captured.
void CalibDisplayJson(JsonValue& out, const CalibCaptureKey& key) {
    out.set("display", JStr(WideToUtf8(key.identity.friendlyName)));
    out.set("edid_id", JStr(WideToUtf8(key.identity.edidId)));
    out.set("captured_monitor", JNum(key.indexAtCapture));
}

void HandleCalibStatus(JsonValue& result) {
    std::vector<CalibCaptureInfo> caps;
    {
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
        caps = g_calib.snapshots.Infos();
    }
    // What exit(restore_snapshot=true) would put back: every display the session captured, the
    // modes it entered there, and how old the capture is. Present even while inactive (a thrown
    // enter keeps its capture). Resolved against the live enumeration only AFTER the calib lock is
    // released: the GUI-thread enter nests settings -> calib, so this pipe-thread reader must
    // never nest calib -> settings (sequential locks, as HandleStateGet does).
    std::vector<CalibLiveMonitor> live;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        live = CalibLiveMonitorsFrom(g_gui.monitorSettings);
    }
    std::vector<CalibCaptureKey> keys;
    for (const CalibCaptureInfo& c : caps) keys.push_back(c.key);
    const std::vector<CalibResolution> where = ResolveCalibCaptures(keys, live);
    const uint64_t now = GetTickCount64();
    JsonValue captures = JArr();
    for (size_t i = 0; i < caps.size(); ++i) {
        JsonValue e = JObj();
        e.set("monitor", where[i].liveIndex >= 0 ? JNum(where[i].liveIndex) : JsonValue());
        e.set("resolved_by", JStr(where[i].how));
        CalibDisplayJson(e, caps[i].key);
        e.set("modes", CalibModesJson(caps[i].sdrEntered, caps[i].hdrEntered));
        e.set("age_s", JNum(now >= caps[i].capturedAtMs ? (double)(now - caps[i].capturedAtMs) / 1000.0 : 0.0));
        captures.arr.push_back(std::move(e));
    }
    result.set("captures", captures);

    // Desktop gamma follows the Windows SDR white level, but never mid-session: monitors (in HDR) whose level
    // moved and whose follow-up is waiting, as of the last check. Applied once nothing holds it (reason).
    JsonValue pending = JArr();
    for (const DesktopGammaSdrWhitePending& w : GetDesktopGammaSdrWhitePending()) {
        JsonValue e = JObj();
        e.set("monitor", JNum(w.monitor));
        e.set("live_nits", JNum(RoundNits(w.liveNits)));
        e.set("recorded_nits", JNum(RoundNits(w.recordedNits)));
        e.set("baked_nits", w.bakedNits > 0.0f ? JNum(RoundNits(w.bakedNits)) : JsonValue());
        e.set("reason", JStr(w.reason));
        pending.arr.push_back(std::move(e));
    }
    result.set("desktop_gamma_sdr_white_pending", pending);
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
    struct MonSnap { HMONITOR hmon; std::wstring friendly; DisplayIdentity identity; int slot; };
    std::vector<MonSnap> snaps;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        snaps.reserve(g_gui.monitors.size());
        for (size_t i = 0; i < g_gui.monitors.size(); ++i) {
            MonSnap s;
            s.hmon = g_gui.monitors[i];
            s.friendly = (i < g_gui.monitorNames.size()) ? g_gui.monitorNames[i] : std::wstring();
            s.slot = -1;
            if (i < g_gui.monitorSettings.size()) {
                s.identity = g_gui.monitorSettings[i].identity;
                s.slot = g_gui.monitorSettings[i].slot;
            }
            snaps.push_back(std::move(s));
        }
    }

    JsonValue arr = JArr();
    for (size_t i = 0; i < snaps.size(); ++i) {
        JsonValue e = JObj();
        e.set("index", JNum((double)i));
        e.set("friendly_name", JStr(WideToUtf8(snaps[i].friendly)));
        // Identity the settings are keyed by (stable across enumeration reorders).
        if (!snaps[i].identity.empty()) {
            e.set("edid_id", JStr(WideToUtf8(snaps[i].identity.edidId)));
            e.set("identity_name", JStr(WideToUtf8(snaps[i].identity.friendlyName)));
        }
        e.set("settings_slot", JNum((double)snaps[i].slot));

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
        const bool haveDi = GetDisplayInfoForMonitor((int)i, di);
        if (haveDi) {
            e.set("device_path", JStr(WideToUtf8(di.devicePath)));
            e.set("hardware_id", JStr(WideToUtf8(ExtractHardwareIdFromPath(di.devicePath))));
            e.set("source_id", JNum((double)di.sourceId));
            e.set("target_id", JNum((double)di.targetId));
            e.set("hdr_capable", JBool(di.isHdrCapable));
            JsonValue adapter = JObj();
            adapter.set("low", JNum((double)di.adapterId.LowPart));
            adapter.set("high", JNum((double)di.adapterId.HighPart));
            e.set("adapter_id", adapter);
            // Live LINK format (DLC 2026-09-26): the bpc/encoding on the cable, which DLC compares
            // against its --bit-depth and the profile's panel.bit_depth. Absent when the query fails
            // or the driver reports 0 bpc (DLC treats absent as "unmeasured", never as a mismatch).
            DisplayLinkFormat lf = QueryDisplayLinkFormat(di);
            if (lf.ok && lf.bitsPerColorChannel > 0) {
                e.set("link_bpc", JNum((double)lf.bitsPerColorChannel));
                e.set("link_color_encoding", JStr(DisplayColorEncodingName(lf.colorEncoding)));
                e.set("link_format_source", JStr(lf.source));
            }
            if (lf.haveOutputTechnology)
                e.set("link_connector", JStr(DisplayOutputTechnologyName(lf.outputTechnology)));
        }

        // Live color space via a fresh DXGI query (the same check the capture
        // path uses) so DLC can confirm the monitor's current mode before a run.
        if (snaps[i].hmon) {
            DXGI_OUTPUT_DESC1 desc;
            if (QueryFreshOutputDesc(snaps[i].hmon, desc)) {
                bool hdrActive = (desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020);
                bool dxgiFp16Sdr = (desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709);
                // ACM ("Automatically manage color for apps") is invisible to DXGI: the output colour
                // space stays G22_P709 with ACM on (HW 2026-09-14, work guide C8), so SDR vs ACM_SDR
                // comes from DisplayConfig (24H2 activeColorMode; older: advancedColorEnabled && !HDR).
                DisplayColorModeResult cm = haveDi ? QueryDisplayColorMode(di, hdrActive, dxgiFp16Sdr)
                                                   : ClassifyDisplayColorMode(hdrActive, dxgiFp16Sdr, false, 0, false, false);
                // hdr_active and color_space from ONE verdict (DisplayConfig can see HDR a moment before DXGI does)
                e.set("hdr_active", JBool(cm.mode == DisplayColorMode::HDR));
                e.set("color_space", JStr(DisplayColorModeName(cm.mode)));
                e.set("color_mode_source", JStr(cm.source));
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
    FaldTrace("EnterNeutral: begin");
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    std::wstring dummy = Utf8ToWide(p.getStr("dummy_icc_path"));
    std::wstring reason = Utf8ToWide(p.getStr("reason"));

    std::wstring removeName;       // the active real MHC profile this enter takes out of scanout
    float identityPeak = 0.0f;
    bool snapshotRetained = false; // true = this session already held a capture of this display
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MonitorSettings& ms = g_gui.monitorSettings[mon];
        {
            std::lock_guard<std::mutex> ck(g_calibMutex);
            // Capture BEFORE clearing, unless this session already captured this display. A
            // session still holding a capture here is one whose display is ALREADY cleared (a run
            // that died without calibration.exit, or an enter that threw part-way): capturing again
            // would overwrite the user's setup with the neutral slate and restore_snapshot would
            // hand back the slate. The store is deliberately NOT cleared on a "fresh" enter (only
            // exit clears it), so a thrown enter cannot lose a valid capture (calib_snapshot.h).
            snapshotRetained = g_calib.snapshots.Enter(g_gui.monitorSettings, mon, isHDR, GetTickCount64());
        }
        MHCSettings& mhc = isHDR ? ms.hdrMHC : ms.sdrMHC;
        if (mhc.enabled && !mhc.profileName.empty()) {
            removeName = mhc.profileName;
            identityPeak = MhcIdentityPeakNits(ms, isHDR);  // before the clears below
        }
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
            ms.hdrColorCorrection.fald.enabled = false;   // a meter must not read through the FALD layer
        } else {
            ms.sdrPath.clear();
            ms.sdrColorCorrection.primariesEnabled = false;
            ms.sdrColorCorrection.grayscale.enabled = false;
            ms.sdrColorCorrection.fald.enabled = false;   // the SDR (ACM) FALD layer likewise
        }
    }
    // Take the active MHC out of scanout: associate the identity MHC2 profile FIRST, then
    // disassociate the real one (Windows keeps applying the last associated MHC2 transform after a
    // bare removal — HW-proven 2026-09-03 / 2026-09-23). Outside the settings lock (file I/O +
    // MSCMS), after mhc.enabled=false so CheckMhcProfiles can't re-assert the old profile. DLC's
    // enter-neutral still associates ITS identity (set_primaries(P)+set_white(D65)+apply) right
    // after; that apply's GenerateAndInstallMhcProfile drops this stand-in again.
    std::wstring identityName;
    if (!removeName.empty()) {
        FaldTrace("EnterNeutral: identity MHC swap");
        identityName = ReplaceMhcProfileWithIdentity(mon, isHDR, identityPeak, removeName);
    }
    FaldTrace("EnterNeutral: settings cleared, SaveSettings");
    SaveSettings();
    FaldTrace("EnterNeutral: UpdateMhcFlagsLive");
    UpdateMhcFlagsLive(mon);
    FaldTrace("EnterNeutral: ReapplyProcessing");
    ReapplyProcessing();
    FaldTrace("EnterNeutral: ReapplyProcessing done");
    // NOTE: dummy-ICC association is deferred to live bring-up; neutrality here comes from the
    // identity-MHC swap above (when an MHC was active) + cleared layers, plus DLC's own `dispwin -c`.

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
    result.set("identity_profile", JStr(WideToUtf8(identityName)));
    // Honest re-enter tell for DLC: true = this call KEPT the session's original capture of this
    // display instead of capturing the cleared state. A build predating the snapshot store omits
    // the field entirely, which is how DLC tells the two apart (additive: no contract bump).
    result.set("snapshot_retained", JBool(snapshotRetained));
}

void DoExitCalibration(const JsonValue& p, JsonValue& result, std::string& error) {
    // Tear down any grayscale live preview left engaged (client died mid-edit) so
    // the panel doesn't stay stuck in preview with PERM_GS stripped.
    CleanupActiveGsLive();
    bool restore = false;
    const JsonValue* rv = p.find("restore_snapshot");
    if (rv && rv->type == JsonValue::Bool) restore = rv->b;
    bool restored = false;
    JsonValue restoredMonitors = JArr();
    JsonValue unrestored = JArr();
    if (restore) {
        // Lock order unchanged: calib -> settings here, settings -> calib in DoEnterNeutral, both
        // on the GUI thread, so the two can never interleave.
        std::lock_guard<std::mutex> ck(g_calibMutex);
        // One MHC action per ENTERED mode of every restored display (see PlanCalibModeRestore). An op
        // points into its capture's pendingMhc, which survives an exception part-way (the store is
        // only cleared below, after everything ran), so a retried exit resumes instead of re-planning.
        struct ModeOp { size_t capture; size_t mode; int monitor; size_t entry; };
        std::vector<ModeOp> ops;
        std::vector<int> touched;
        {
            std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
            // Resolve every capture to its display's CURRENT index (the enumeration may have
            // shifted since the enter) and plan against the live settings as they are now,
            // before anything is copied back.
            const CalibRestorePlan plan = PlanCalibRestore(g_calib.snapshots, g_gui.monitorSettings);
            for (const CalibRestoreStep& step : plan.steps) {
                CalibCapture& cap = g_calib.snapshots.captures[step.capture];
                MonitorSettings& live = g_gui.monitorSettings[(size_t)step.liveIndex];
                // The identity profile's peak comes from the settings the session left live, read
                // before the copy drops them (as DoEnterNeutral computes it).
                std::vector<CalibModeRestore> modes = step.modes;
                if (!step.resumed)
                    for (CalibModeRestore& mr : modes)
                        if (mr.action == CalibMhcRestore::IdentitySwap) mr.livePeak = MhcIdentityPeakNits(live, mr.isHdr);
                // The captured settings go back; the live identity / slot / legacyIndex stay.
                for (size_t k : ApplyCalibRestoreStep(cap, live, step, modes))
                    ops.push_back(ModeOp{ step.capture, k, step.liveIndex, restoredMonitors.arr.size() });
                touched.push_back(step.liveIndex);
                JsonValue e = JObj();
                e.set("monitor", JNum(step.liveIndex));
                e.set("resolved_by", JStr(step.how));
                if (step.resumed) e.set("resumed", JBool(true));
                CalibDisplayJson(e, cap.key);
                e.set("modes", CalibModesJson(cap.sdrEntered, cap.hdrEntered));
                restoredMonitors.arr.push_back(std::move(e));
            }
            for (const CalibUnrestored& u : plan.unrestored) {
                const CalibCapture& cap = g_calib.snapshots.captures[u.capture];
                JsonValue e = JObj();
                e.set("reason", JStr(u.reason));
                CalibDisplayJson(e, cap.key);
                e.set("modes", CalibModesJson(cap.sdrEntered, cap.hdrEntered));
                unrestored.arr.push_back(std::move(e));
            }
        }
        if (!touched.empty()) {
            SaveSettings();
            // Per entered mode: which MHC action ran and whether it landed. `restored` means the
            // SETTINGS went back; a failed reinstall / identity swap leaves the calibration's
            // transform in scanout, so DLC must be able to see it (restored_monitors[].mhc).
            std::vector<JsonValue> mhcResults(restoredMonitors.arr.size(), JArr());
            for (const ModeOp& op : ops) {
                CalibModeRestore& mr = g_calib.snapshots.captures[op.capture].pendingMhc[op.mode];
                // Reinstall the original MHC of every mode the session entered. Where there was
                // none but the calibration left one associated (DLC's identity or an interim
                // build), swap it for the identity profile rather than leaving it applied: it is no
                // longer referenced by settings, so the stale-association sweep would drop it →
                // nothing associated, while Windows keeps applying its transform (HW-proven
                // 2026-09-03 / 2026-09-23).
                bool ok = true;
                const char* action = "none";
                if (mr.action == CalibMhcRestore::Reinstall) {
                    action = "reinstall";
                    ok = GenerateAndInstallMhcProfile(op.monitor, mr.isHdr);
                } else if (mr.action == CalibMhcRestore::IdentitySwap) {
                    action = "identity_swap";
                    ok = !ReplaceMhcProfileWithIdentity(op.monitor, mr.isHdr, mr.livePeak, mr.liveProfileName).empty();
                }
                mr.done = true;   // ran (a returned failure is reported, not retried)
                JsonValue r = JObj();
                r.set("mode", JStr(mr.isHdr ? "HDR" : "SDR"));
                r.set("action", JStr(action));
                r.set("ok", JBool(ok));
                if (op.entry < mhcResults.size()) mhcResults[op.entry].arr.push_back(std::move(r));
            }
            for (size_t k = 0; k < mhcResults.size(); ++k)
                restoredMonitors.arr[k].set("mhc", std::move(mhcResults[k]));
            for (int mon : touched) UpdateMhcFlagsLive(mon);
            ReapplyProcessing();
            restored = true;
        }
    }
    {
        std::lock_guard<std::mutex> ck(g_calibMutex);
        g_calib.active = false;
        g_calib.correctionsReset = false;
        // The session is over either way. On the apply path (restore_snapshot=false) the
        // calibrated state is what the user now has, so the pre-session captures must not survive
        // into a later exit(restore) — the 2026-09-27 bug: `hasSnapshot` was never cleared, so a
        // `3dlut-only --abort` (a flow that never enters) restored a PREVIOUS run's pre-run
        // snapshot over the user's current setup.
        g_calib.snapshots.Clear();
    }
    // Desktop gamma waited for the session: catch up with the SDR white level now (debounced re-check).
    if (g_gui.hwndMain) SetTimer(g_gui.hwndMain, SDR_WHITE_CHECK_TIMER_ID, SDR_WHITE_CHECK_DEBOUNCE_MS, nullptr);
    result.set("active", JBool(false));
    result.set("restored", JBool(restored));
    // Which displays went back (at their CURRENT index), and any the session captured but could
    // not put back (disconnected, or EDID twins it cannot tell apart). Always present.
    result.set("restored_monitors", restoredMonitors);
    result.set("unrestored", unrestored);
}

// layers.set — toggle the viewing layers of one monitor:mode over the pipe, exactly as the
// GUI checkboxes do (DLC captures them before a run, measures with them OFF, restores after;
// the user should never have to manage corrections around a pipeline run). Params: monitor,
// mode, and any of white_balance / grayscale / desktop_gamma / tonemap (bool; omitted = keep).
//   * white_balance / grayscale / desktop_gamma live in the MHC layer: set the flags, then ONE
//     RegenerateMhcIfActive (BuildMHC2Params bakes all three), like ID_MHC_*_WB_ENABLE /
//     ID_MHC_*_GS_ENABLE. Desktop Gamma also drives the live gamma atomics (ID_MHC_HDR_DG_ENABLE).
//   * tonemap (HDR) is the shader flag: ID_CORR_TONEMAP_ENABLE's live update path.
//   * fald is the shader flag of the addressed mode (HDR, or SDR under ACM): ID_CORR_FALD_[SDR_]ENABLE's path.
// Result: {monitor_mode, before:{...}, after:{...}, regenerated, profile_name}.
static void LayersJson(const MonitorSettings& s, bool isHDR, JsonValue& out) {
    const MHCSettings& m = isHDR ? s.hdrMHC : s.sdrMHC;
    out.set("white_balance", JBool(m.whiteBalanceEnabled));
    out.set("grayscale", JBool(m.correctionGrayscale.enabled));
    out.set("desktop_gamma", JBool(isHDR && m.desktopGammaEnabled));
    out.set("tonemap", JBool(isHDR && s.hdrColorCorrection.tonemap.enabled));
    const FaldSettings& fs = isHDR ? s.hdrColorCorrection.fald : s.sdrColorCorrection.fald;   // per mode
    out.set("fald", JBool(fs.enabled));
    out.set("fald_params_path", JStr(WideToUtf8(fs.paramsPath)));
}

void DoLayersSet(const JsonValue& p, JsonValue& result, std::string& error) {
    FaldTrace("LayersSet: begin");
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    auto want = [&](const char* name, bool& has, bool& val) {
        const JsonValue* v = p.find(name);
        has = (v && v->type == JsonValue::Bool);
        if (has) val = v->b;
    };
    bool hasWb = false, wb = false, hasGs = false, gs = false, hasDg = false, dg = false,
         hasTm = false, tm = false, hasFd = false, fd = false;
    want("white_balance", hasWb, wb);
    want("grayscale", hasGs, gs);
    want("desktop_gamma", hasDg, dg);
    want("tonemap", hasTm, tm);
    want("fald", hasFd, fd);
    if (!isHDR && (hasDg || hasTm)) {
        // HDR-only layers; asking to change them in SDR is a client error, asking
        // for them OFF is a harmless no-op (a "disable everything" client). fald is per mode.
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
        FaldSettings& fs = isHDR ? ms.hdrColorCorrection.fald : ms.sdrColorCorrection.fald;
        if (hasFd && fs.enabled != fd) { fs.enabled = fd; tmChanged = true; }   // same propagation as tonemap, for the addressed mode
        mhcEnabled = m.enabled;
        profileNamed = !m.profileName.empty();
    }
    FaldTrace("LayersSet: settings updated");
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
        FaldTrace(g_gui.isRunning ? "LayersSet: tm/fald changed, running -> UpdateColorCorrectionLive" : "LayersSet: tm/fald changed, not running");
        if (g_gui.isRunning) {
            UpdateColorCorrectionLive(mon, isHDR);   // tonemap is HDR-only; fald follows the addressed mode
            FaldTrace("LayersSet: after UpdateColorCorrectionLive");
            if (g_dwmHookMode.load()) UpdateDwmHookSharedConfig();
            else DwmHookReevaluateOverlay();
            FaldTrace("LayersSet: after reevaluate");
        } else if ((hasTm && tm) || (hasFd && fd)) {
            FaldTrace("LayersSet: StartProcessing");
            StartProcessing();
        }
    }
    if (mhcChanged || tmChanged) {
        FaldTrace("LayersSet: UpdateMhcFlagsLive");
        UpdateMhcFlagsLive(mon);
        FaldTrace("LayersSet: SaveSettings");
        SaveSettings();
        FaldTrace("LayersSet: UpdateGUIState");
        UpdateGUIState();
        FaldTrace("LayersSet: UpdateColorCorrectionControls");
        UpdateColorCorrectionControls();   // the GUI checkboxes follow the pipe
        FaldTrace("LayersSet: controls updated");
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
            s.hdrColorCorrection.fald.enabled = false;
            s.sdrColorCorrection.fald.enabled = false;
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
        // BuildMHC2Params resolves the display primaries through primariesPreset and only
        // reads customPrimaries for the "Custom" slot. Leaving the preset where the GUI last
        // put it (0 = sRGB on a fresh monitor) silently baked sRGB+D65 as the display
        // primaries — an IDENTITY matrix — and ignored both these values and set_white
        // (LG C6 HDR, 2026-09-04: measured primaries stored, profile still identity).
        m.primariesPreset = g_numPresetPrimaries - 1;
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
        GrayscaleSettings& slot = correction ? m.correctionGrayscale : m.baseGrayscale;
        if (!ApplyGrayscalePayload(slot, p, isHDR, error)) return;
        result.set("point_count", JNum(slot.pointCount));   // what was stored
    }
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("mhc", JObj());
}

// Import a full-resolution 1D .cube as the MHC base grayscale/EOTF correction — the
// path ColourSpace/DisplayCal use via the GUI file-import, now reachable over the pipe
// so DLC can carry a dense per-channel TRC instead of the coarse 10/20/32-point editable table
// (src/grayscale_validate.h), which is far too sparse for a PQ EOTF. Sets sourceFilePath/
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
    // No `enabled = true` pre-set here: GenerateAndInstallMhcProfile sets it on success, and retires a
    // named-but-disabled old profile (after mhc.remove / calibration.enter) by itself. Pre-setting it
    // opened a window in which the whitelist monitor saw enabled + the REMOVED profile's name + the
    // identity stand-in as default and re-asserted the removed profile; a failed apply also left the
    // removed profile marked active.
    // GenerateAndInstallMhcProfile snapshots settings under the mutex internally,
    // so it MUST be called without g_monitorSettingsMutex held.
    if (!GenerateAndInstallMhcProfile(mon, isHDR)) {
        error = "GenerateAndInstallMhcProfile failed";
        return;
    }
    {
        // Refresh the Display Calibration panel's "Primaries / Gamma / Peak" labels the way
        // the GUI Apply button does (gui.cpp ID_MHC_*_APPLY); a pipe apply used to leave them
        // describing the previous profile (e.g. "Rec.2020 / PQ / 10000" over a DLC install).
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MHCSettings& m = isHDR ? g_gui.monitorSettings[mon].hdrMHC : g_gui.monitorSettings[mon].sdrMHC;
        ComputeMhcMetadata(m, isHDR);
    }
    // Pipe handlers run on the GUI thread (WM_CALIB_CMD marshalling), so the panel can be
    // repainted directly, exactly as the GUI Apply path does.
    UpdateMhcInfoDisplay(mon, isHDR);
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
    std::wstring oldName;
    float identityPeak = 0.0f;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        MHCSettings& m = isHDR ? g_gui.monitorSettings[mon].hdrMHC : g_gui.monitorSettings[mon].sdrMHC;
        oldName = m.profileName;
        identityPeak = MhcIdentityPeakNits(g_gui.monitorSettings[mon], isHDR);
        m.enabled = false;
    }
    // Like the GUI Remove button: associate the identity MHC2 profile FIRST, then disassociate the
    // real one — Windows keeps applying the last associated MHC2 transform after a bare removal
    // (HW-proven 2026-09-03 / 2026-09-23). Done outside the settings lock (file I/O + MSCMS calls).
    std::wstring identityName;
    if (!oldName.empty())
        identityName = ReplaceMhcProfileWithIdentity(mon, isHDR, identityPeak, oldName);
    UpdateMhcFlagsLive(mon);
    SaveSettings();
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("removed", JBool(true));
    result.set("identity_profile", JStr(WideToUtf8(identityName)));
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

// FALD correction layer, per mode (HDR, and SDR under Windows ACM — 2026-09-14, work guide P7).
// runtime.set_fald_params {monitor, mode:"HDR"|"SDR", params_path}: the per-panel parameter file (DLC
// `python -m dlc.fald.export`) for that mode; its transfer must match (PQ file <-> HDR, gamma file
// <-> SDR: refused otherwise — the render side would refuse it too). Re-setting the SAME path bumps
// reloadSeq so a file re-exported in place rebuilds. runtime.fald_debug {monitor, mode,
// debug_mode 0..6}: 0 correct, 1 gain map (white 0, red brighten, blue darken, +-25 %), 2 B_true, 3 B_est, 4 identity passthrough, 5 pedestal term
// x100, 6 per-channel-vs-white influence x100, 7 temporal settling, 8 the black-frame boost's non-black zone map (FLD4 files), 9 the starfield balancing zone map (not persisted); optional ped_mode 0|1 (persisted; the GUI "Per-channel pedestal" toggle: 1 = subtract the
// FLD2 file's pedestal colour per channel, 0 = white pedestal as before). runtime.fald_dump {monitor, mode, dir}: next frame writes drive/B_true/B_est/
// frame (input) + fald_out (output) dumps to dir (reference comparison against the Python model).
// Each of these also reaches the screen on a static desktop (render thread re-processes the last frame).
static void FaldPropagate(int mon, bool isHDR) {
    FaldTrace("FaldPropagate: begin");
    if (g_gui.isRunning) {
        UpdateColorCorrectionLive(mon, isHDR);
        if (g_dwmHookMode.load()) {
            UpdateDwmHookSharedConfig();
            RequestFaldFullRecompose();   // on: prime the hook's clean copy now; off: clear the corrected pixels
        }
        else DwmHookReevaluateOverlay();
    }
    FaldTrace("FaldPropagate: UpdateColorCorrectionControls");
    UpdateColorCorrectionControls();   // the GUI's View combo / Panel file box follow the pipe
    FaldTrace("FaldPropagate: end");
}

void DoSetFaldParams(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    std::wstring path = Utf8ToWide(p.getStr("params_path"));
    if (path.empty()) { error = "missing parameter: params_path"; return; }
    DWORD attrs = GetFileAttributesW(path.c_str());
    if (attrs == INVALID_FILE_ATTRIBUTES || (attrs & FILE_ATTRIBUTE_DIRECTORY)) { error = "params_path is not a file"; return; }
    // A readable panel file must be a fit for this mode (FLD1/FLD2 = PQ = HDR; FLD3 word 40 = 1 = gamma = SDR
    // under ACM). A file the peek cannot classify is accepted here and refused by the loader (logged once).
    uint32_t transfer = 0;
    const bool known = FaldPanelFileTransfer(path, transfer);
    if (known && !FaldTransferMatchesMode(transfer, isHDR)) {
        error = std::string("panel file transfer ") + (transfer == FALD_TRANSFER_GAMMA ? "gamma (SDR fit)" : "pq (HDR fit)") +
                " does not match mode " + (isHDR ? "HDR" : "SDR");   // DLC mock: identical text
        return;
    }
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        FaldSettings& fs = isHDR ? g_gui.monitorSettings[mon].hdrColorCorrection.fald : g_gui.monitorSettings[mon].sdrColorCorrection.fald;
        fs.paramsPath = path;
        fs.reloadSeq++;   // same path re-set = "reload the file" (HW 2026-09-13: it did not rebuild before)
    }
    SaveSettings();
    FaldPropagate(mon, isHDR);
    FaldPanelFileChangedReinject();   // hook mode: the DLL reads panel files only at injection
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("params_path", JStr(WideToUtf8(path)));
    result.set("transfer", JStr(known ? (transfer == FALD_TRANSFER_GAMMA ? "gamma" : "pq") : "unknown"));
}

void DoFaldDebug(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    const JsonValue* v = p.find("debug_mode");
    const JsonValue* pm = p.find("ped_mode");
    if ((!v || v->type != JsonValue::Num) && (!pm || pm->type != JsonValue::Num)) { error = "missing parameter: debug_mode (0..9) or ped_mode (0|1)"; return; }
    unsigned int mode = 0, ped = 0;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        FaldSettings& fs = isHDR ? g_gui.monitorSettings[mon].hdrColorCorrection.fald : g_gui.monitorSettings[mon].sdrColorCorrection.fald;
        if (v && v->type == JsonValue::Num) fs.debugMode = (unsigned int)(v->num < 0 ? 0 : (v->num > 9 ? 9 : v->num));
        if (pm && pm->type == JsonValue::Num) fs.pedMode = (pm->num >= 0.5) ? 1u : 0u;
        mode = fs.debugMode; ped = fs.pedMode;
    }
    if (pm && pm->type == JsonValue::Num) SaveSettings();   // ped_mode is a persisted setting, the debug view is not
    FaldPropagate(mon, isHDR);
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("debug_mode", JNum((double)mode));
    result.set("ped_mode", JNum((double)ped));
    std::wstring pathNow;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        pathNow = (isHDR ? g_gui.monitorSettings[mon].hdrColorCorrection.fald : g_gui.monitorSettings[mon].sdrColorCorrection.fald).paramsPath;
    }
    result.set("ped_colour_in_file", JBool(FaldPanelFileHasPedColour(pathNow)));   // false = an FLD1 file: ped_mode 1 is a no-op
}

// runtime.fald_temporal {monitor, mode, temporal_mode?, tau_rise_ms?, tau_fall_ms?, delay_frames?, closure?, parity?}: the
// shader's per-cell drive state (LED-lag filter; fald.h FALD_TEMPORAL_*, DLC dlc/fald/temporal.py). temporal_mode 3 =
// the measured panel clock (work guide C13, DLC dlc/fald/paneltime.py) with closure (share of the LED gap closed per
// tick) and parity (-1 unknown / 0 / 1). Persisted per mode like ped_mode (the GUI row sets both modes). Error texts are
// mirrored by the DLC mock word for word.
void DoFaldTemporal(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    const JsonValue* tm = p.find("temporal_mode");
    const JsonValue* tr = p.find("tau_rise_ms");
    const JsonValue* tf = p.find("tau_fall_ms");
    const JsonValue* df = p.find("delay_frames");
    const JsonValue* cl = p.find("closure");
    const JsonValue* pa = p.find("parity");
    auto isNum = [](const JsonValue* v) { return v && v->type == JsonValue::Num; };
    if (!isNum(tm) && !isNum(tr) && !isNum(tf) && !isNum(df) && !isNum(cl) && !isNum(pa)) { error = "missing parameter: temporal_mode (0|1|2|3), tau_rise_ms, tau_fall_ms (0..5000), delay_frames (0..3), closure (0.05..1) or parity (-1|0|1)"; return; }
    if (isNum(tm) && !(tm->num == 0 || tm->num == 1 || tm->num == 2 || tm->num == 3)) { error = "temporal_mode must be 0 (off), 1 (both fields), 2 (B_true only) or 3 (panel clock)"; return; }
    if (isNum(tr) && !(tr->num >= 0 && tr->num <= FALD_TAU_MAX_MS)) { error = "tau_rise_ms must be 0..5000 ms"; return; }
    if (isNum(tf) && !(tf->num >= 0 && tf->num <= FALD_TAU_MAX_MS)) { error = "tau_fall_ms must be 0..5000 ms"; return; }
    if (isNum(df) && !(df->num == 0 || df->num == 1 || df->num == 2 || df->num == 3)) { error = "delay_frames must be 0..3"; return; }
    if (isNum(cl) && !(cl->num >= FALD_CLOCK_CLOSURE_MIN && cl->num <= FALD_CLOCK_CLOSURE_MAX)) { error = "closure must be 0.05..1"; return; }
    if (isNum(pa) && !(pa->num == -1 || pa->num == 0 || pa->num == 1)) { error = "parity must be -1 (unknown), 0 or 1"; return; }
    unsigned int mode = 0, delay = 0; float rise = 0.0f, fall = 0.0f, closure = FALD_CLOCK_CLOSURE_DEFAULT; int parity = -1;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        FaldSettings& fs = isHDR ? g_gui.monitorSettings[mon].hdrColorCorrection.fald : g_gui.monitorSettings[mon].sdrColorCorrection.fald;
        if (isNum(tm)) fs.temporalMode = (unsigned int)tm->num;
        if (isNum(tr)) fs.tauRiseMs = (float)tr->num;
        if (isNum(tf)) fs.tauFallMs = (float)tf->num;
        if (isNum(df)) fs.delayFrames = (unsigned int)df->num;
        if (isNum(cl)) fs.clockClosure = (float)cl->num;
        if (isNum(pa)) fs.clockParity = (int)pa->num;
        mode = fs.temporalMode; rise = fs.tauRiseMs; fall = fs.tauFallMs; delay = fs.delayFrames;
        closure = fs.clockClosure; parity = fs.clockParity;
    }
    SaveSettings();
    FaldPropagate(mon, isHDR);
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("temporal_mode", JNum((double)mode));
    result.set("tau_rise_ms", JNum((double)rise));
    result.set("tau_fall_ms", JNum((double)fall));
    result.set("delay_frames", JNum((double)delay));
    result.set("closure", JNum((double)closure));
    result.set("parity", JNum((double)parity));
    // mode 3 settles in refreshes (independent of the rate); the first-order modes in frames at 60 Hz
    result.set("settle_frames_60hz", JNum((double)(mode == FALD_TEMPORAL_PANEL ? FaldPanelClockSettleFrames(closure)
                                                                               : FaldSettleFrames(rise, fall, 1000.0f / 60.0f, delay))));
}

// runtime.fald_starfield {monitor, mode, enabled?, even?, lift?, target_gain?, target_sigma?, keep_nits?, even_reach?, cap_nits?, strength?,
// area_lo?, area_hi?, peak_hi?, reach?, nb_lo?, nb_hi?}: starfield balancing (EXPERIMENT, default off; work guide S1,
// reference DLC dlc/fald/starfield.py). Partial updates; persisted per mode (the GUI row sets both modes). Every value
// is validated BEFORE anything is stored; error texts are mirrored by the DLC mock word for word.
void DoFaldStarfield(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    struct NumKey { const char* name; double lo, hi; bool integer; const char* text; };
    static const NumKey keys[] = {
        { "even", 0.0, 1.0, false, "even must be 0..1" },
        { "lift", 0.0, 1.0, false, "lift must be 0..1" },
        { "target_gain", 0.05, 2.0, false, "target_gain must be 0.05..2" },
        { "even_reach", 0.0, (double)FALD_STAR_EVEN_REACH_MAX, true, "even_reach must be an integer 0..12" },
        { "cap_nits", 0.0, 10000.0, false, "cap_nits must be 0..10000 (0 = none)" },
        { "strength", 0.0, 1.0, false, "strength must be 0..1" },
        { "area_lo", 0.0, 1.0e6, false, "area_lo must be 0..1000000 px^2" },
        { "area_hi", 0.0, 1.0e6, false, "area_hi must be 0..1000000 px^2" },
        { "peak_hi", 0.0, 10000.0, false, "peak_hi must be 0..10000 (0 = no limit)" },
        { "reach", 0.0, (double)FALD_STAR_REACH_MAX, true, "reach must be an integer 0..4" },
        { "nb_lo", 0.0, 1.0, false, "nb_lo must be 0..1" },
        { "nb_hi", 0.0, 1.0, false, "nb_hi must be 0..1" },
        { "target_sigma", 0.0, 4.0, false, "target_sigma must be 0..4" },
        { "keep_nits", 0.0, 10000.0, false, "keep_nits must be 0..10000 (0 = no floor)" },
    };
    const size_t nKeys = sizeof(keys) / sizeof(keys[0]);
    const JsonValue* en = p.find("enabled");
    if (en && en->type != JsonValue::Bool) { error = "enabled must be a boolean"; return; }
    const JsonValue* vals[sizeof(keys) / sizeof(keys[0])] = {};
    bool any = (en != nullptr);
    for (size_t i = 0; i < nKeys; i++) {
        const JsonValue* v = p.find(keys[i].name);
        if (!v || v->type != JsonValue::Num) continue;
        if (!(v->num >= keys[i].lo && v->num <= keys[i].hi) || (keys[i].integer && v->num != (double)(long long)v->num)) {
            error = keys[i].text; return;
        }
        vals[i] = v; any = true;
    }
    if (!any) {
        error = "missing parameter: enabled, even, lift, target_gain, target_sigma, keep_nits, even_reach, cap_nits, strength, area_lo, area_hi, peak_hi, reach, nb_lo or nb_hi";
        return;
    }
    FaldStarfieldSettings out;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        FaldSettings& fs = isHDR ? g_gui.monitorSettings[mon].hdrColorCorrection.fald : g_gui.monitorSettings[mon].sdrColorCorrection.fald;
        FaldStarfieldSettings st = fs.star;
        if (en) st.enabled = en->b;
        float* fdst[] = { &st.even, &st.lift, &st.targetGain, nullptr, &st.capNits, &st.strength, &st.areaLo, &st.areaHi,
                          &st.peakHi, nullptr, &st.nbLo, &st.nbHi, &st.targetSigma, &st.keepNits };
        for (size_t i = 0; i < nKeys; i++) {
            if (!vals[i]) continue;
            if (fdst[i]) *fdst[i] = (float)vals[i]->num;
            else if (i == 3) st.evenReach = (unsigned int)vals[i]->num;
            else st.reach = (unsigned int)vals[i]->num;
        }
        // the pairs must stay ordered after a partial update (the stored partner counts)
        if (st.areaHi < st.areaLo) { error = "area_hi must be >= area_lo"; return; }
        if (st.nbHi < st.nbLo) { error = "nb_hi must be >= nb_lo"; return; }
        FaldStarfieldClamp(st);
        fs.star = st;
        out = st;
    }
    SaveSettings();
    FaldPropagate(mon, isHDR);
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("enabled", JBool(out.enabled));
    result.set("even", JNum((double)out.even));
    result.set("lift", JNum((double)out.lift));
    result.set("target_gain", JNum((double)out.targetGain));
    result.set("target_sigma", JNum((double)out.targetSigma));
    result.set("keep_nits", JNum((double)out.keepNits));
    result.set("even_reach", JNum((double)out.evenReach));
    result.set("cap_nits", JNum((double)out.capNits));
    result.set("strength", JNum((double)out.strength));
    result.set("area_lo", JNum((double)out.areaLo));
    result.set("area_hi", JNum((double)out.areaHi));
    result.set("peak_hi", JNum((double)out.peakHi));
    result.set("reach", JNum((double)out.reach));
    result.set("nb_lo", JNum((double)out.nbLo));
    result.set("nb_hi", JNum((double)out.nbHi));
}

void DoFaldDump(const JsonValue& p, JsonValue& result, std::string& error) {
    int mon; bool isHDR;
    if (!ParseMonitorMode(p, mon, isHDR, error)) return;
    std::wstring dir = Utf8ToWide(p.getStr("dir"));
    if (dir.empty()) { error = "missing parameter: dir"; return; }
    if (GetFileAttributesW(dir.c_str()) == INVALID_FILE_ATTRIBUTES) { error = "dir does not exist"; return; }
    bool found = false;
    {
        std::lock_guard<std::mutex> lk(g_monitorsMutex);
        for (auto& ctx : g_monitors) {
            if (ctx.index == mon) {
                // the dump is of the layer that runs, i.e. the monitor's live mode: a request for the other mode would
                // silently dump the wrong settings/file
                if (ctx.isHDREnabled != isHDR) {
                    error = std::string("monitor is in ") + (ctx.isHDREnabled ? "HDR" : "SDR") + ", not " + (isHDR ? "HDR" : "SDR");
                    return;
                }
                if (ctx.faldDumpRequested.load()) { error = "a fald dump is still pending for this monitor"; return; }
                ctx.faldDumpDir = dir;                                            // written before the flag ...
                ctx.faldDumpRequested.store(true, std::memory_order_release);     // ... which publishes it
                ctx.redrawRequested.store(true);                                  // fire on a static desktop too
                found = true; break;
            }
        }
    }
    if (!found) { error = "monitor context not running"; return; }
    result.set("monitor_mode", JStr(MonitorModeKey(mon, isHDR)));
    result.set("dir", JStr(WideToUtf8(dir)));
    result.set("note", JStr("written by the render thread on the next frame the FALD layer runs; look for fald_dump.txt"));
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

// hook.set_routing {action: swap|confirm|clear|assign|identify, monitor?, entries?} — see BuildHookStateJson.
// identify: run an identity-beacon session (blocking, <= DWM_HOOK_BEACON_MAX_MS) and report the
//       routing it produced; no re-injection.
// swap: the monitor's position and its single same-size/same-bpc twin's exchange every recorded
//       context, then re-inject (the DLL honours the pins). confirm: mark the current assignment
//       meter-verified (no re-inject). clear: delete the file and re-inject (a fresh roll).
//       assign: explicit {ctx,left,top} list for rigs with more than one twin.
void DoHookSetRouting(const JsonValue& p, JsonValue& result, std::string& error) {
    std::string action = p.getStr("action");
    bool reinject = false;
    if (action == "confirm") {
        DwmHookRouting r = ReadDwmHookRouting();
        if (!r.present) { error = "no hook routing state to confirm (the hook has not assigned any context yet)"; return; }
        if (r.stale) { error = "hook routing state is stale (dwm.exe restarted) - re-check before confirming"; return; }
        r.confirmed = true;
        if (!WriteDwmHookRouting(r)) { error = "failed to rewrite the hook routing file"; return; }
    } else if (action == "clear") {
        if (!ClearDwmHookRouting()) { error = "failed to delete the hook routing file"; return; }
        reinject = true;
    } else if (action == "identify") {
        if (!g_dwmHookMode.load() || !g_gui.isRunning) { error = "DWM hook mode is not running"; return; }
        if (!DwmHookHasTwinMonitors()) { error = "no indistinguishable twin monitors - nothing to identify"; return; }
        RunDwmHookBeaconBlocking(g_gui.hwndMain, "hook.set_routing identify");
    } else if (action == "swap") {
        const JsonValue* mv = p.find("monitor");
        if (!mv || mv->type != JsonValue::Num) { error = "missing parameter: monitor"; return; }
        int mon = (int)std::llround(mv->num);
        int left = 0, top = 0;
        {
            std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
            if (mon < 0 || mon >= (int)g_gui.monitors.size()) { error = "monitor index out of range"; return; }
            MONITORINFO info = { sizeof(info) };
            if (!GetMonitorInfo(g_gui.monitors[mon], &info)) { error = "GetMonitorInfo failed"; return; }
            left = info.rcMonitor.left;
            top = info.rcMonitor.top;
        }
        DwmHookRouting r = ReadDwmHookRouting();
        if (!r.present) { error = "no hook routing state to swap (the hook has not assigned any context yet)"; return; }
        if (r.stale) { error = "hook routing state is stale (dwm.exe restarted) - re-inject first"; return; }
        std::wstring err = SwapDwmHookRouting(r, left, top);
        if (!err.empty()) { error = WideToUtf8(err); return; }
        r.confirmed = false;
        if (!WriteDwmHookRouting(r)) { error = "failed to rewrite the hook routing file"; return; }
        reinject = true;
    } else if (action == "assign") {
        const JsonValue* ev = p.find("entries");
        if (!ev || ev->type != JsonValue::Arr || ev->arr.empty()) { error = "missing parameter: entries"; return; }
        DwmHookRouting r = ReadDwmHookRouting();
        if (!r.present) { error = "no hook routing state to assign (the hook has not assigned any context yet)"; return; }
        if (r.stale) { error = "hook routing state is stale (dwm.exe restarted) - re-inject first"; return; }
        for (const auto& item : ev->arr) {
            std::string ctx = item.getStr("ctx");
            if (ctx.size() > 2 && ctx[0] == '0' && (ctx[1] == 'x' || ctx[1] == 'X')) ctx = ctx.substr(2);
            for (auto& ch : ctx) ch = (char)tolower((unsigned char)ch);
            const JsonValue* lv = item.find("left");
            const JsonValue* tv = item.find("top");
            if (ctx.empty() || !lv || !tv || lv->type != JsonValue::Num || tv->type != JsonValue::Num) {
                error = "each entry needs ctx, left, top"; return;
            }
            int left = (int)std::llround(lv->num), top = (int)std::llround(tv->num);
            bool onTopology = false;
            for (const auto& m : r.monitors)
                if (m.left == left && m.top == top) { onTopology = true; break; }
            if (!onTopology) { error = "position is not in the hook's recorded topology: " + ctx; return; }
            bool found = false;
            for (auto& e : r.entries) {
                if (e.ctx != ctx) continue;
                e.left = left;
                e.top = top;
                found = true;
            }
            if (!found) { error = "unknown context " + ctx; return; }
        }
        r.confirmed = false;
        if (!WriteDwmHookRouting(r)) { error = "failed to rewrite the hook routing file"; return; }
        reinject = true;
    } else {
        error = "unknown action (swap|confirm|clear|assign|identify)";
        return;
    }
    if (reinject) {
        // Eject + re-inject: the DLL reloads the (rewritten / deleted) routing file at attach
        // and rewrites it as it re-resolves each context. Wait (bounded) for the DLL's OWN
        // rewrite — every entry the host wrote resolved and none left as a fresh roll — so the
        // state we report is the new assignment, not the file we just wrote.
        size_t written = ReadDwmHookRouting().entries.size();
        ReapplyProcessing();
        reinject = g_gui.isRunning.load();   // nothing to re-inject when no correction is active
        for (int i = 0; reinject && i < 60; ++i) {
            Sleep(50);
            DwmHookRouting now = ReadDwmHookRouting();
            if (!now.present) continue;
            bool settled = (action == "clear") ? !now.entries.empty() : now.entries.size() >= written;
            for (const auto& e : now.entries) {
                if (e.method == "order" && action != "clear") settled = false;
                if (e.method == "provisional") settled = false;   // a replacement guess still being resolved
            }
            if (settled) break;
        }
    }
    result.set("hook", BuildHookStateJson());
    result.set("reinjected", JBool(reinject));
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
        if (!ApplyGrayscalePayload(cc.grayscale, *tweak, isHDR, error)) return;  // sets enabled = true
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

    // `mon` indexes monitorSettings after EnsureProcessingForPreview's message pumps: a display
    // change pumped meanwhile must not re-attach the vector underneath (deferred instead).
    MonitorSettingsPin pin;
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
        if (!ApplyGrayscalePayload(m.correctionGrayscale, *gs, isHDR, error)) return;  // sets enabled = true
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
           m.rfind("runtime.", 0) == 0 ||  // set_3dlut / clear_3dlut / *_grayscale_tweak
           m.rfind("hook.", 0) == 0;       // set_routing (re-injects => GUI thread)
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
            if (t_clientPipe)
                std::cerr << "[Calibration IPC] " << method << " from "
                          << ipc_client::PipeClientDescription(t_clientPipe) << std::endl;
            if (!g_gui.hwndMain) {
                error = "GUI window not available";
            } else {
                auto call = std::make_shared<CalibGuiCall>();
                call->method = method;
                call->params = params;
                uint64_t id;
                {
                    std::lock_guard<std::mutex> lk(g_guiCallsMutex);
                    id = g_nextGuiCallId++;
                    g_guiCalls[id] = call;
                }
                DWORD w = WAIT_FAILED;
                const bool posted = call->doneEvent &&
                                    PostMessageW(g_gui.hwndMain, WM_CALIB_CMD, (WPARAM)id, 0);
                if (posted) {
                    HANDLE waits[2] = { call->doneEvent, g_stopEvent };
                    w = WaitForMultipleObjects(g_stopEvent ? 2 : 1, waits, FALSE, kGuiTimeoutMs);
                }
                {
                    // From here on a still-queued WM_CALIB_CMD finds nothing and runs nothing.
                    std::lock_guard<std::mutex> lk(g_guiCallsMutex);
                    g_guiCalls.erase(id);
                }
                if (call->done.load(std::memory_order_acquire)) {
                    result = std::move(call->result);
                    error = call->error;
                } else if (!posted) {
                    error = "GUI thread did not accept the command";
                } else if (w == WAIT_OBJECT_0 + 1) {
                    // Disarmed. Not started = never runs; already running (nested in a pumping
                    // GUI handler) = finishes into the call it co-owns. Unknown either way.
                    error = "calibration control was disarmed (the command may not have run; re-read state)";
                } else {
                    // Still running on the GUI thread (it keeps its own reference) or never
                    // reached: the outcome is unknown, so say so rather than report success.
                    error = "GUI thread did not respond in time (the command may still complete; re-read state)";
                }
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
HANDLE g_serverThread = nullptr;

bool ServerEnabled() {
    // In-app toggle (Settings checkbox / tray) — the human's deliberate arming
    // action. Persisted, so a checkbox left on re-arms at next launch.
    if (g_calibrationControlEnabled.load()) return true;
    // Headless/dev/CI enable: env var or a flag file next to the exe.
    // DESKTOPLUT_CALIBRATION=1 (or true/yes/on) arms; anything else — including a value too
    // long for the buffer, which GetEnvironmentVariableW reports by NOT writing it — does not.
    wchar_t env[8] = {0};
    DWORD n = GetEnvironmentVariableW(L"DESKTOPLUT_CALIBRATION", env, 8);
    if (n > 0 && n < 8) {
        if (wcscmp(env, L"1") == 0 || _wcsicmp(env, L"true") == 0 ||
            _wcsicmp(env, L"yes") == 0 || _wcsicmp(env, L"on") == 0) return true;
    }
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

// ---- Overlapped pipe I/O with deadlines -------------------------------------
// The pipe is a single instance, so a client that connects and then stalls (never sends its line,
// never reads the reply — e.g. a DLC call that timed out and orphaned its connection) used to wedge
// the server for good: blocking ReadFile/WriteFile, FlushFileBuffers waiting for a reader, and a Stop
// whose self-connect failed with ERROR_PIPE_BUSY. Every wait below is bounded and also ends on the
// stop event.
constexpr DWORD kRequestReadDeadlineMs = 15000;   // the client writes its line right after connecting
constexpr DWORD kReplyWriteDeadlineMs  = 15000;
constexpr DWORD kClientCloseWaitMs     = 5000;    // the client closes after reading its reply line

enum class IoResult { Done, Failed, TimedOut, Stopped };

// Waits for an overlapped operation that was just started (`started` = its return value).
IoResult FinishOverlapped(HANDLE pipe, OVERLAPPED& ov, BOOL started, DWORD timeoutMs, DWORD& bytes) {
    bytes = 0;
    if (!started) {
        const DWORD e = GetLastError();
        if (e != ERROR_IO_PENDING) return IoResult::Failed;
    }
    HANDLE waits[2] = { ov.hEvent, g_stopEvent };
    const DWORD w = WaitForMultipleObjects(2, waits, FALSE, timeoutMs);
    if (w != WAIT_OBJECT_0) {
        CancelIoEx(pipe, &ov);
        GetOverlappedResult(pipe, &ov, &bytes, TRUE);   // the cancelled op must finish before `ov` dies
        return (w == WAIT_OBJECT_0 + 1) ? IoResult::Stopped : IoResult::TimedOut;
    }
    return GetOverlappedResult(pipe, &ov, &bytes, FALSE) ? IoResult::Done : IoResult::Failed;
}

struct OverlappedEvent {
    OVERLAPPED ov{};
    OverlappedEvent() { ov.hEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr); }
    ~OverlappedEvent() { if (ov.hEvent) CloseHandle(ov.hEvent); }
    OVERLAPPED& Reset() { HANDLE e = ov.hEvent; ov = OVERLAPPED{}; ov.hEvent = e; ResetEvent(e); return ov; }
    OverlappedEvent(const OverlappedEvent&) = delete;
    OverlappedEvent& operator=(const OverlappedEvent&) = delete;
};

bool WriteAllOverlapped(HANDLE pipe, OverlappedEvent& io, const std::string& data, DWORD deadlineTick) {
    size_t off = 0;
    while (off < data.size()) {
        const DWORD now = GetTickCount();
        if ((LONG)(deadlineTick - now) <= 0) return false;
        OVERLAPPED& ov = io.Reset();
        DWORD n = 0;
        const BOOL ok = WriteFile(pipe, data.data() + off, (DWORD)(data.size() - off), nullptr, &ov);
        if (FinishOverlapped(pipe, ov, ok, deadlineTick - now, n) != IoResult::Done || n == 0) return false;
        off += n;
    }
    return true;
}

// Returns false when the server thread must stop serving (it could not drop a client's identity).
bool HandleConnection(HANDLE pipe, OverlappedEvent& io, const ipc_client::TokenIdentity& server) {
    std::string request;
    char buf[4096];
    bool gotLine = false;
    const DWORD readDeadline = GetTickCount() + kRequestReadDeadlineMs;
    while (request.size() < kMaxRequestBytes) {
        const DWORD now = GetTickCount();
        if ((LONG)(readDeadline - now) <= 0) return true;   // stalled client: drop it
        OVERLAPPED& ov = io.Reset();
        DWORD n = 0;
        const BOOL ok = ReadFile(pipe, buf, sizeof(buf), nullptr, &ov);
        if (FinishOverlapped(pipe, ov, ok, readDeadline - now, n) != IoResult::Done || n == 0) break;
        request.append(buf, n);
        size_t nl = request.find('\n');
        if (nl != std::string::npos) { request.resize(nl); gotLine = true; break; }
    }
    if (!gotLine && request.empty()) return true;   // closed / failed before sending anything

    // Per-connection client check (T2.2). After the read: the client's context is captured by then.
    bool revertFailed = false;
    const ipc_client::TokenIdentity client = ipc_client::ReadPipeClientIdentity(pipe, revertFailed);
    if (revertFailed) {
        std::cerr << "[Calibration IPC] could not revert from a client's identity; server stopped" << std::endl;
        return false;
    }
    const ipc_client::Verdict verdict = ipc_client::Judge(client, server);

    std::string response;
    if (verdict != ipc_client::Verdict::Allowed) {
        std::cerr << "[Calibration IPC] rejected a client (" << ipc_client::PipeClientDescription(pipe)
                  << "): " << ipc_client::VerdictText(verdict) << std::endl;
        response = ErrResponse(std::string("client not permitted: ") + ipc_client::VerdictText(verdict)) + "\n";
    } else if (request.size() >= kMaxRequestBytes) {
        response = ErrResponse("request too large") + "\n";
    } else {
        t_clientPipe = pipe;   // Dispatch logs who sent each mutating verb
        response = Dispatch(request) + "\n";
        t_clientPipe = nullptr;
    }
    if (!WriteAllOverlapped(pipe, io, response, GetTickCount() + kReplyWriteDeadlineMs)) return true;
    // Instead of FlushFileBuffers (which blocks until the client has read everything — forever for a
    // client that never reads): wait, bounded, for the client to close its end after reading the reply,
    // so the disconnect does not discard data it is still reading.
    OVERLAPPED& ov = io.Reset();
    DWORD n = 0;
    const BOOL ok = ReadFile(pipe, buf, sizeof(buf), nullptr, &ov);
    FinishOverlapped(pipe, ov, ok, kClientCloseWaitMs, n);
    return true;
}

DWORD WINAPI ServerThreadProc(LPVOID) {
    PSECURITY_DESCRIPTOR sd = BuildLocalUserSd();
    if (!sd) {
        // Fail closed: without the user+SYSTEM DACL the pipe would get the default named-pipe
        // descriptor (read access for Everyone/anonymous) on an elevated host.
        std::cerr << "[Calibration IPC] could not build the pipe's security descriptor; server not started"
                  << std::endl;
        return 0;
    }
    SECURITY_ATTRIBUTES sa{sizeof(sa), sd, FALSE};
    // Fail closed: without our own identity no client can be judged.
    const ipc_client::TokenIdentity serverIdentity = ipc_client::ReadProcessTokenIdentity();
    if (!serverIdentity.valid) {
        std::cerr << "[Calibration IPC] could not read the process token; server not started" << std::endl;
        LocalFree(sd);
        return 0;
    }
    OverlappedEvent io;
    if (!io.ov.hEvent) { LocalFree(sd); return 0; }
    DWORD lastCreateError = 0;
    bool keepServing = true;
    while (keepServing && WaitForSingleObject(g_stopEvent, 0) != WAIT_OBJECT_0) {
        // FILE_FLAG_FIRST_PIPE_INSTANCE: never serve under a name another process created first.
        HANDLE pipe = CreateNamedPipeW(
            kPipeName,
            PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED | FILE_FLAG_FIRST_PIPE_INSTANCE,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,                       // single instance — one client at a time
            64 * 1024, 64 * 1024,
            0,
            &sa);
        if (pipe == INVALID_HANDLE_VALUE) {
            const DWORD e = GetLastError();
            if (e != lastCreateError) {   // once per distinct failure, not every 500 ms
                std::cerr << "[Calibration IPC] CreateNamedPipe failed (error " << e
                          << (e == ERROR_ACCESS_DENIED ? ", name already in use" : "") << "); retrying"
                          << std::endl;
                lastCreateError = e;
            }
            if (WaitForSingleObject(g_stopEvent, 500) == WAIT_OBJECT_0) break;
            continue;
        }
        lastCreateError = 0;

        OVERLAPPED& ov = io.Reset();
        bool connected = false;
        if (ConnectNamedPipe(pipe, &ov)) {
            connected = true;
        } else {
            const DWORD e = GetLastError();
            if (e == ERROR_PIPE_CONNECTED) {
                connected = true;
            } else if (e == ERROR_IO_PENDING) {
                DWORD n = 0;
                connected = (FinishOverlapped(pipe, ov, FALSE, INFINITE, n) == IoResult::Done);
            }
        }
        if (connected && WaitForSingleObject(g_stopEvent, 0) != WAIT_OBJECT_0)
            keepServing = HandleConnection(pipe, io, serverIdentity);
        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);
    }
    LocalFree(sd);
    return 0;
}

}  // namespace

// ===========================================================================
// Public entry points
// ===========================================================================
bool IsCalibrationOrLiveEditActive() {
    // Sequential, never nested: the GUI-thread enter nests settings -> calib.
    {
        std::lock_guard<std::mutex> lk(g_calibMutex);
        if (g_calib.active) return true;
    }
    std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
    for (const auto& kv : g_gsLive)
        if (kv.second.active) return true;
    return false;
}

void StartCalibrationIpcServer() {
    if (g_serverThread) {
        if (!g_stopEvent || WaitForSingleObject(g_stopEvent, 0) != WAIT_OBJECT_0)
            return;   // the live server — already armed
        // A stopped server still finishing a slow request (its stop event is set, so it is on its way
        // out): start the new one only once it is gone — two would race for the single pipe instance.
        if (WaitForSingleObject(g_serverThread, 10000) != WAIT_OBJECT_0) {
            std::cerr << "[Calibration IPC] previous server still finishing a request; not re-armed "
                         "(toggle Calibration control again)" << std::endl;
            return;
        }
        CloseHandle(g_serverThread);
        g_serverThread = nullptr;
    }
    if (!ServerEnabled()) return;  // SECURITY: opt-in only
    if (!g_stopEvent) return;
    ResetEvent(g_stopEvent);
    g_serverThread = CreateThread(nullptr, 0, ServerThreadProc, nullptr, 0, nullptr);
}

void StopCalibrationIpcServer() {
    if (!g_serverThread) return;
    // Ends every pipe wait (cancelling a stalled client's I/O) and every pending GUI-call wait, so the
    // server never needs this (GUI) thread to exit: a plain wait, deliberately NOT pumping — Stop runs
    // from WM_DESTROY, where dispatching queued messages would run handlers mid-teardown.
    if (g_stopEvent) SetEvent(g_stopEvent);
    if (WaitForSingleObject(g_serverThread, 5000) == WAIT_OBJECT_0) {
        CloseHandle(g_serverThread);
        g_serverThread = nullptr;
    }
    // else: still inside a slow read-only handler (e.g. a DisplayConfig flip); it exits on its own
    // (the stop event stays set), and a re-arm waits for it (StartCalibrationIpcServer).
}

LRESULT HandleCalibrationGuiCommand(WPARAM wParam, LPARAM /*lParam*/) {
    std::shared_ptr<CalibGuiCall> call;
    {
        std::lock_guard<std::mutex> lk(g_guiCallsMutex);
        auto it = g_guiCalls.find((uint64_t)wParam);
        if (it == g_guiCalls.end()) return 0;   // stale (caller gave up) or not ours
        call = it->second;
    }
    const JsonValue& params = call->params;
    JsonValue& result = call->result;
    std::string& error = call->error;
    try {
        const std::string& m = call->method;
        if (m == "calibration.enter") DoEnterNeutral(params, result, error);
        else if (m == "calibration.exit") DoExitCalibration(params, result, error);
        else if (m == "corrections.disable_all") DoDisableAll(params, result, error);
        else if (m == "layers.set") DoLayersSet(params, result, error);
        else if (m == "mhc.set_primaries") DoMhcSetPrimaries(params, result, error);
        else if (m == "mhc.set_white") DoMhcSetWhite(params, result, error);
        else if (m == "mhc.set_base_grayscale") DoMhcSetGrayscale(params, result, error, false);
        else if (m == "mhc.set_base_lut") DoMhcSetBaseLut(params, result, error);
        else if (m == "mhc.set_correction_grayscale") DoMhcSetGrayscale(params, result, error, true);
        else if (m == "mhc.grayscale_live_begin") DoGrayscaleLiveBegin(params, result, error);
        else if (m == "mhc.grayscale_set_live") DoGrayscaleSetLive(params, result, error);
        else if (m == "mhc.grayscale_commit") DoGrayscaleCommit(params, result, error);
        else if (m == "mhc.grayscale_cancel") DoGrayscaleCancel(params, result, error);
        else if (m == "mhc.apply") DoMhcApply(params, result, error);
        else if (m == "mhc.remove") DoMhcRemove(params, result, error);
        else if (m == "runtime.set_3dlut") DoSet3dlut(params, result, error);
        else if (m == "runtime.clear_3dlut") DoClear3dlut(params, result, error);
        else if (m == "runtime.set_fald_params") DoSetFaldParams(params, result, error);
        else if (m == "runtime.fald_debug") DoFaldDebug(params, result, error);
        else if (m == "runtime.fald_dump") DoFaldDump(params, result, error);
        else if (m == "runtime.fald_temporal") DoFaldTemporal(params, result, error);
        else if (m == "runtime.fald_starfield") DoFaldStarfield(params, result, error);
        else if (m == "hook.set_routing") DoHookSetRouting(params, result, error);
        else if (m == "runtime.set_grayscale_tweak") DoSetGrayscaleTweak(params, result, error);
        else if (m == "runtime.disable_grayscale_tweak") DoDisableGrayscaleTweak(params, result, error);
        else error = "unknown method: " + m;
    } catch (const std::exception& e) {
        error = std::string("exception: ") + e.what();
    } catch (...) {
        error = "unhandled exception";
    }
    call->done.store(true, std::memory_order_release);
    if (call->doneEvent) SetEvent(call->doneEvent);
    return 0;
}
