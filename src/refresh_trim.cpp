// DesktopLUT - refresh_trim.cpp
// The per-refresh-rate grey trim (refresh_trim.h): resolve each display's trim from its DLC DIP and its live refresh
// rate, and put the HDR MHC profile variant stamped with it on screen.

#include "refresh_trim.h"

#include "actions/notify.h"
#include "desktoplut_ipc_server.h"   // IsCalibrationOrLiveEditActive
#include "displayconfig.h"
#include "dlc/dlc_join.h"            // HardwareIdOf
#include "dlc/dlc_scan.h"
#include "globals.h"
#include "gui.h"
#include "gui_mhc.h"
#include "mhc.h"
#include "settings.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <iostream>
#include <locale>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>

// ---- pure ------------------------------------------------------------------------------------------------------------

namespace {

void Fnv(uint64_t& h, const void* data, size_t n) {
    const unsigned char* p = static_cast<const unsigned char*>(data);
    for (size_t i = 0; i < n; i++) { h ^= p[i]; h *= 1099511628211ull; }
}

void FnvDouble(uint64_t& h, double v) { Fnv(h, &v, sizeof(v)); }

std::string Upper(std::string s) {
    for (char& c : s) c = (char)toupper((unsigned char)c);
    return s;
}

std::string FmtHz(double hz) {
    // "47.952", "60", "59.94": the catalogue's spelling of a rate (3 decimals, trailing zeros dropped)
    char b[32];
    snprintf(b, sizeof(b), "%.3f", hz);
    std::string s = b;
    while (!s.empty() && s.back() == '0') s.pop_back();
    if (!s.empty() && s.back() == '.') s.pop_back();
    return s;
}

}  // namespace

std::string RefreshTrimStamp(const MhcRefreshTrim& t) {
    uint64_t h = 1469598103934665603ull;
    static const char kTag[] = "dlut.refresh_trim.bake/2";   // bump with any change to what a trim bakes (2: the fade above)
    Fnv(h, kTag, sizeof(kTag) - 1);
    Fnv(h, t.rateKey.data(), t.rateKey.size());
    FnvDouble(h, t.peakNits);
    const uint64_t n = (uint64_t)(std::min)(t.nits.size(), t.lnGain.size());
    Fnv(h, &n, sizeof(n));
    for (size_t i = 0; i < n; i++) {
        FnvDouble(h, t.nits[i]);
        FnvDouble(h, t.lnGain[i]);
    }
    char b[17];
    snprintf(b, sizeof(b), "%016llx", (unsigned long long)h);
    return b;
}

bool DeriveRefreshTrim(const dlc::DipRefreshRate& rate, double peakNits, MhcRefreshTrim& out, std::string* why) {
    out = MhcRefreshTrim{};
    auto fail = [&](const std::string& w) { if (why) *why = w; out = MhcRefreshTrim{}; return false; };
    for (const dlc::DipRefreshLevel& lv : rate.levels) {
        if (!(std::isfinite(lv.nits) && lv.nits > 0.0)) return fail("rate " + rate.key + ": a level without nits > 0");
        // A ratio this far from 1 is not a refresh effect a grey trim may follow (DLC measured 1.00 .. 1.035).
        if (!(std::isfinite(lv.ratio) && lv.ratio >= 0.5 && lv.ratio <= 2.0))
            return fail("rate " + rate.key + ": a ratio outside 0.5 .. 2");
        if (!out.nits.empty() && !(lv.nits > out.nits.back())) continue;   // levels ascending: a repeat is dropped
        out.nits.push_back(lv.nits);
        out.lnGain.push_back(-std::log(lv.ratio));
    }
    if (out.nits.empty()) return fail("rate " + rate.key + ": no levels");
    out.active = true;
    out.rateKey = rate.key;
    out.rateHz = rate.hz;
    out.peakNits = (std::isfinite(peakNits) && peakNits > 0.0) ? peakNits : 0.0;
    out.stamp = RefreshTrimStamp(out);
    return true;
}

RefreshTrimChoice ChooseRefreshTrim(const dlc::DipRefreshSection& s, double liveHz) {
    RefreshTrimChoice c;
    if (!s.error.empty()) { c.pick = RefreshTrimPick::Invalid; c.why = s.error; return c; }
    if (!(liveHz > 0.0)) { c.pick = RefreshTrimPick::Unmeasured; c.why = "refresh rate unknown"; return c; }
    const double refHz = s.referenceDen ? (double)s.referenceNum / (double)s.referenceDen : 60.0;
    int best = -1;
    double bestD = 1e30;
    for (size_t i = 0; i < s.rates.size(); i++) {
        const double d = std::fabs(s.rates[i].hz - liveHz);
        if (d < bestD) { bestD = d; best = (int)i; }
    }
    const double refD = std::fabs(refHz - liveHz);
    if (refD <= kRefreshTrimRateTolHz && refD <= bestD) {
        c.pick = RefreshTrimPick::Reference;
        c.why = FmtHz(liveHz) + " Hz is the reference";
        return c;
    }
    if (best < 0 || bestD > kRefreshTrimRateTolHz) {
        c.pick = RefreshTrimPick::Unmeasured;
        c.why = FmtHz(liveHz) + " Hz is not measured";
        return c;
    }
    c.matched = best;
    int cur = best;
    for (int depth = 0;; depth++) {
        const std::string& same = s.rates[cur].sameAs;
        if (same.empty()) break;
        if (same == s.referenceKey) {
            c.pick = RefreshTrimPick::Reference;
            c.why = s.rates[best].key + " Hz = " + s.referenceKey + " Hz (measured)";
            return c;
        }
        int next = -1;
        for (size_t i = 0; i < s.rates.size(); i++) if (s.rates[i].key == same) next = (int)i;
        if (next < 0 || depth >= 8) {
            c.pick = RefreshTrimPick::Invalid;
            c.why = next < 0 ? "rate " + s.rates[cur].key + ": same_as " + same + " is not in the section"
                             : "rate " + s.rates[best].key + ": same_as goes round in a circle";
            return c;
        }
        cur = next;
    }
    c.curve = cur;
    if (s.rates[cur].levels.empty()) {
        c.pick = RefreshTrimPick::Invalid;
        c.why = "rate " + s.rates[cur].key + ": no levels";
        return c;
    }
    c.pick = RefreshTrimPick::Trim;
    c.why = cur == best ? s.rates[best].key + " Hz" : s.rates[best].key + " Hz = " + s.rates[cur].key + " Hz";
    return c;
}

int FindRefreshSection(const std::vector<dlc::DipRefreshSection>& sections, const std::string& mode,
                       const RefreshTrimLiveId& self, const std::vector<RefreshTrimLiveId>& live, std::string* why) {
    const std::string hw = Upper(self.hardwareId);
    if (hw.empty()) { if (why) *why = "the display's hardware id is unknown"; return -1; }
    int sameModel = 0;
    for (const RefreshTrimLiveId& l : live) if (Upper(l.hardwareId) == hw) sameModel++;
    int best = -1;
    for (size_t i = 0; i < sections.size(); i++) {
        const dlc::DipRefreshSection& s = sections[i];
        if (dlc::NormMode(s.mode) != dlc::NormMode(mode) || Upper(s.hardwareId) != hw) continue;
        // Twin panels: only the one on the connector it was measured on.
        if (sameModel > 1 && (s.devicePath.empty() || _wcsicmp(s.devicePath.c_str(), self.devicePath.c_str()) != 0))
            continue;
        if (best < 0) { best = (int)i; continue; }
        const dlc::DipRefreshSection& b = sections[best];
        // A usable section before an unusable one, then the newest measured.
        if (s.error.empty() != b.error.empty()) { if (s.error.empty()) best = (int)i; continue; }
        if (s.measured > b.measured) best = (int)i;
    }
    if (best < 0 && why)
        *why = sameModel > 1 ? "no DIP refresh response measured on this connector (" + hw + " twin panels)"
                             : "no DIP refresh response for " + hw + " in " + dlc::NormMode(mode);
    return best;
}

RefreshTrimResolution ResolveRefreshTrim(bool enabled, bool haveFolder, bool dipFound,
                                         const std::vector<dlc::DipRefreshSection>& sections,
                                         const RefreshTrimLiveId& self, const std::vector<RefreshTrimLiveId>& live,
                                         double liveHz, double fallbackPeakNits) {
    RefreshTrimResolution r;
    if (!haveFolder) { r.state = "no_dlc"; r.detail = "no DLC folder (the DLC tab's, or next to DesktopLUT)"; }
    else if (!dipFound) { r.state = "no_dip"; r.detail = "no DIP (dip_store.json) in the DLC folder"; }
    else {
        std::string why;
        r.section = FindRefreshSection(sections, "HDR", self, live, &why);
        if (r.section < 0) {
            r.state = "no_section";
            r.detail = why;
        } else {
            const dlc::DipRefreshSection& s = sections[(size_t)r.section];
            const RefreshTrimChoice ch = ChooseRefreshTrim(s, liveHz);
            r.detail = ch.why;
            switch (ch.pick) {
            case RefreshTrimPick::Invalid: r.state = "invalid"; break;
            case RefreshTrimPick::Reference: r.state = "reference"; break;
            case RefreshTrimPick::Unmeasured: r.state = "unmeasured"; break;
            case RefreshTrimPick::Trim: {
                const double peak = std::isfinite(s.peakNits) && s.peakNits > 0.0 ? s.peakNits : fallbackPeakNits;
                if (DeriveRefreshTrim(s.rates[(size_t)ch.curve], peak, r.want, &why)) {
                    r.state = "trim";
                    r.want.rateHz = s.rates[(size_t)ch.curve].hz;
                } else {
                    r.state = "invalid";
                    r.detail = why;
                }
                break;
            }
            }
        }
    }
    if (!enabled) {   // the switch: nothing baked; the DIP's answer stays in the detail (the tooltip)
        r.want = MhcRefreshTrim{};
        r.state = "off";
    }
    return r;
}

// ---- runtime ---------------------------------------------------------------------------------------------------------

namespace {

std::atomic<bool> g_requestPending{ false };
std::mutex g_statusMutex;
std::vector<RefreshTrimStatus> g_status;

// The startup read (InitRefreshTrim), used until the first DLC folder scan publishes. GUI thread.
bool g_primed = false;
std::wstring g_primedFolder;
bool g_primedDip = false;
dlc::DipStoreData g_primedData;

bool g_exiting = false;
int g_failures = 0;
ULONGLONG g_retryNotBefore = 0;
bool g_warned = false;
std::map<int, std::string> g_lastLogged;   // per monitor: the last logged state line

constexpr UINT kWaitRetryMs = 3000;          // a hold (calibration, live edit, dialog, maintenance)
constexpr UINT kRateRetryMs = 1000;          // the refresh rate could not be read (mid-modeset)
constexpr UINT kFailRetryMs[] = { 2000, 5000, 15000, 60000, 300000 };
constexpr int kWarnAfterFailures = 3;

void ArmRetry(UINT ms) {
    if (g_gui.hwndMain && !g_appShuttingDown.load()) SetTimer(g_gui.hwndMain, REFRESH_TRIM_TIMER_ID, ms, nullptr);
}

std::wstring ExeDir() {
    wchar_t exe[MAX_PATH] = {};
    const DWORD n = GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring dir(exe, n);
    const size_t slash = dir.find_last_of(L"\\/");
    return slash == std::wstring::npos ? std::wstring() : dir.substr(0, slash);
}

std::string Today() {
    SYSTEMTIME st;
    GetLocalTime(&st);
    char b[16];
    snprintf(b, sizeof(b), "%04u-%02u-%02u", st.wYear, st.wMonth, st.wDay);
    return b;
}

std::wstring Wide(const std::string& s) { return dlc::Utf8ToWide(s); }

// The section's rates, one line each, as the tooltip and state.get show them.
std::vector<std::string> RateLines(const dlc::DipRefreshSection& s) {
    std::vector<std::string> out;
    for (const dlc::DipRefreshRate& r : s.rates) {
        if (!r.sameAs.empty()) out.push_back(r.key + " Hz = " + r.sameAs);
        else if (!r.levels.empty())
            out.push_back(r.key + " Hz trim (" + std::to_string(r.levels.size()) + " levels)");
        else out.push_back(r.key + " Hz (no levels)");
    }
    return out;
}

}  // namespace

std::vector<RefreshTrimStatus> GetRefreshTrimStatus() {
    std::lock_guard<std::mutex> lk(g_statusMutex);
    return g_status;
}

void RequestRefreshTrim() {
    if (g_requestPending.exchange(true)) return;   // one message carries every request made before it runs
    if (!g_gui.hwndMain || !PostMessage(g_gui.hwndMain, WM_REFRESH_TRIM, 0, 0)) g_requestPending.store(false);
}

void RequestRefreshTrimDipScan() {
    // The worker resolves the folder (the candidates' file checks never run on this thread) and scans it.
    dlc::RequestScan(g_dlcFolder, dlc::FolderCandidates(ExeDir(), g_dlcFolderLast));
}

void InitRefreshTrim() {
    dlc::SetDipListener([]() { RequestRefreshTrim(); });
    // One synchronous read before the first bakes (the worker's scan answers asynchronously): the folder the scan
    // would read, its dip_store.json only.
    bool detected = false;
    g_primedFolder = dlc::ResolveFolder(g_dlcFolder, dlc::FolderCandidates(ExeDir(), g_dlcFolderLast), &detected);
    uint64_t hash = 0;
    g_primedDip = dlc::ReadDipStore(g_primedFolder, g_primedData, hash);
    g_primed = true;
    std::wcout << L"[Refresh trim] DIP " << (g_primedDip ? L"read from " : L"not found in ")
               << (g_primedFolder.empty() ? std::wstring(L"(no DLC folder)") : g_primedFolder) << L": "
               << g_primedData.refresh.size() << L" refresh response(s)" << std::endl;
    RequestRefreshTrimDipScan();
}

bool SetRefreshTrimEnabled(int monitor, bool on) {
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (monitor < 0 || monitor >= (int)g_gui.monitorSettings.size()) return false;
        g_gui.monitorSettings[monitor].hdrMHC.refreshTrimEnabled = on;
    }
    std::cout << "[Refresh trim] monitor " << monitor << ": switched " << (on ? "on" : "off") << std::endl;
    SaveSettings();
    RequestRefreshTrimDipScan();   // the switch doubles as a reload of the DIP
    ReconcileRefreshTrim("switch");
    GuiNotifySettingsChanged(monitor);
    return true;
}

void ReleaseRefreshTrimForExit(bool sessionEnding) {
    // Without DesktopLUT nothing follows the rate: the untrimmed (60-Hz) profile is the one that is right at the
    // reference rate, and only a trim's residual off at the others — leave that one on. A session end keeps what is
    // there (DesktopLUT runs again from the next sign-in and resolves it at its start).
    if (sessionEnding) return;
    g_exiting = true;
    ReconcileRefreshTrim("exit");
    // No trimmed variant is left behind in the cache either (forgotten by name; the next start's orphan cleanup deletes
    // the files): the saved INI then names only untrimmed variants, safe for a build without the trim, and the next
    // run bakes a trimmed one again at its first switch to a trimmed rate.
    int forgotten = 0;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        auto forget = [&forgotten](MHCSettings& m) {
            for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
                if (m.permTrimStamp[k].empty() || m.permNames[k] == m.profileName) continue;
                m.permNames[k].clear();
                m.permPaths[k].clear();
                m.permTrimStamp[k].clear();
                forgotten++;
            }
            m.trimStash.clear();
        };
        for (MonitorSettings& ms : g_gui.monitorSettings) forget(ms.hdrMHC);
        for (MonitorSettings& ms : g_gui.parkedSettings) forget(ms.hdrMHC);
    }
    if (forgotten > 0) {
        std::cout << "[Refresh trim] exit: " << forgotten << " trimmed cached variant(s) forgotten" << std::endl;
        SaveSettings();
    }
}

void ReconcileRefreshTrim(const char* why) {
    g_requestPending.store(false);
    if (g_appShuttingDown.load() && !g_exiting) return;
    const char* reason = why ? why : "";
    const bool periodic = strcmp(reason, "periodic") == 0;

    // The DIP: the newest scan's, else the startup read.
    const std::shared_ptr<const dlc::Data> d = dlc::Current();
    const bool scanned = d->request != 0 || !g_primed;
    const bool haveFolder = scanned ? !d->folder.empty() : !g_primedFolder.empty();
    const bool dipFound = scanned ? d->dipFound : g_primedDip;
    const std::vector<dlc::DipRefreshSection>& sections = scanned ? d->dip.refresh : g_primedData.refresh;
    const std::string today = Today();

    // Holds (read before the settings lock: IsCalibrationOrLiveEditActive takes it). None at exit.
    const char* hold = nullptr;
    if (!g_exiting) {
        if (IsCalibrationOrLiveEditActive()) hold = "calibration";
        else if (g_mhcEditDialogOpen.load()) hold = "mhc_dialog_open";
        else if (g_monitorSettingsPins > 0) hold = "settings_pinned";
    }

    struct Entry {
        int index = -1;
        HMONITOR hmon = nullptr;
        RefreshTrimLiveId id;
        bool enabled = true;
        double fallbackPeak = 0.0;
    };
    std::vector<Entry> entries;
    std::vector<RefreshTrimLiveId> live;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
            const MonitorSettings& ms = g_gui.monitorSettings[i];
            Entry e;
            e.index = (int)i;
            e.hmon = i < g_gui.monitors.size() ? g_gui.monitors[i] : nullptr;
            e.id.hardwareId = dlc::HardwareIdOf(ms.identity.edidId, ms.identity.devicePath);
            e.id.devicePath = ms.identity.devicePath;
            e.enabled = ms.hdrMHC.refreshTrimEnabled && !g_exiting;
            e.fallbackPeak = MhcProfileMetadataPeakNits(ms.hdrMHC, true);
            entries.push_back(e);
            live.push_back(e.id);
        }
    }

    std::vector<RefreshTrimStatus> status;
    bool wait = false, failed = false, swapped = false, changed = false, rateUnknown = false;
    const bool inBackoff = periodic && GetTickCount64() < g_retryNotBefore;
    for (const Entry& e : entries) {
        RefreshTrimStatus st;
        st.monitor = e.index;
        st.enabled = e.enabled;
        DisplayInfo di;
        UINT32 num = 0, den = 0;
        const bool haveRate = e.hmon && ResolveDisplayInfoForHMonitor(e.hmon, di) && QueryDisplayRefreshRate(di, num, den);
        st.num = num;
        st.den = den;
        st.liveHz = haveRate ? (double)num / (double)den : 0.0;
        const RefreshTrimResolution res =
            ResolveRefreshTrim(e.enabled, haveFolder, dipFound, sections, e.id, live, st.liveHz, e.fallbackPeak);
        st.state = res.state;
        st.detail = res.detail;
        if (res.section >= 0) {
            const dlc::DipRefreshSection& s = sections[(size_t)res.section];
            st.recordKey = s.recordKey;
            st.measured = s.measured;
            st.runId = s.runId;
            st.ageDays = dlc::DaysBetween(s.measured, today);
            st.stale = st.ageDays > (s.maxAgeDays > 0 ? s.maxAgeDays : 180);
            st.rates = RateLines(s);
        }
        if (res.want.active) {
            st.rateKey = res.want.rateKey;
            st.wantStamp = res.want.stamp;
            st.gain[0] = MhcRefreshTrimGain(res.want, 0.5);
            st.gain[1] = MhcRefreshTrimGain(res.want, 100.0);
            st.gain[2] = MhcRefreshTrimGain(res.want, 1000.0);
        }
        // Mid-modeset the rate may be unreadable: keep what is wanted now, ask again shortly. (At exit: none.)
        const bool resolved = haveRate || g_exiting;
        if (!resolved) {
            rateUnknown = true;
            if (st.state == "trim" || st.state == "reference" || st.state == "unmeasured") st.state = "no_rate";
        }

        bool needSwap = false, installed = false, preview = false;
        std::string current, wantForActive;
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            if (e.index >= (int)g_gui.monitorSettings.size()) continue;
            MHCSettings& m = g_gui.monitorSettings[(size_t)e.index].hdrMHC;
            current = m.refreshTrimWant.active ? m.refreshTrimWant.stamp : std::string();
            if (resolved && !hold && current != st.wantStamp) {
                m.refreshTrimWant = res.want.active ? res.want : MhcRefreshTrim{};
                changed = true;
            }
            installed = m.enabled && !m.profileName.empty();
            preview = !m.previewScanoutName.empty();
            st.profileName = m.profileName;
            const std::string expected = MhcExpectedTrimStamp(m, true, m.activePerm);
            st.activeStamp = installed ? (m.permNames[m.activePerm] == m.profileName ? m.permTrimStamp[m.activePerm]
                                                                                       : std::string("?"))
                                       : std::string();
            // What the profile on screen should carry for the rate (the want just resolved, the switch, the stage).
            wantForActive = (res.want.active && !(NormalizeMhcPerm(m.activePerm) & MHCSettings::PERM_NOGRAY))
                                ? res.want.stamp : std::string();
            // A profile on screen that is not its slot's (only a hand-edited INI does that) is left alone unless a
            // trim is wanted: a display without a trim never swaps here.
            needSwap = installed && st.activeStamp != expected && (st.activeStamp != "?" || !expected.empty());
        }
        st.installed = installed;
        st.applied = installed ? (st.activeStamp == wantForActive || (st.activeStamp == "?" && wantForActive.empty()))
                               : !res.want.active;
        if (!installed && res.want.active) st.detail += " (no HDR MHC profile installed to carry it)";

        if (needSwap) {
            if (hold || preview) {
                st.waiting = preview ? "live_edit" : hold;
                wait = true;
            } else if (inBackoff) {
                st.waiting = "retry_backoff";
            } else {
                // The processing thread's start pass deletes profile files its settings snapshot does not name, and
                // re-associates the active profile: never swap beside it.
                std::unique_lock<std::mutex> maintenance(g_mhcMaintenanceMutex, std::try_to_lock);
                if (!maintenance.owns_lock()) {
                    st.waiting = "mhc_maintenance";
                    wait = true;
                } else {
                    std::cout << "[Refresh trim] monitor " << e.index << ": "
                              << (res.want.active ? "the " + res.want.rateKey + " Hz trim" : std::string("no trim"))
                              << " at " << FmtHz(st.liveHz) << " Hz, swapping the HDR profile (" << reason << ")"
                              << std::endl;
                    if (SwapMhcPermBits(e.index, true, 0, 0)) {
                        swapped = true;
                        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                        if (e.index < (int)g_gui.monitorSettings.size()) {
                            const MHCSettings& m = g_gui.monitorSettings[(size_t)e.index].hdrMHC;
                            st.profileName = m.profileName;
                            st.activeStamp = m.permNames[m.activePerm] == m.profileName ? m.permTrimStamp[m.activePerm]
                                                                                         : std::string("?");
                            st.applied = st.activeStamp == wantForActive;
                        }
                    } else {
                        failed = true;
                        st.waiting = "retry_backoff";
                    }
                    if (!g_exiting) {
                        const std::wstring title = !failed && res.want.active
                            ? L"Refresh trim " + Wide(res.want.rateKey) + L" Hz"
                            : !failed ? L"Refresh trim off at " + Wide(FmtHz(st.liveHz)) + L" Hz"
                                      : L"Refresh trim swap failed";
                        std::wstringstream detail;
                        detail.imbue(std::locale::classic());
                        if (failed) {
                            detail << L"The HDR profile stays as it was; retrying";
                        } else {
                            detail << L"HDR profile " << st.profileName;
                            if (res.want.active) {
                                detail.precision(3);
                                detail << std::fixed << L"; gain " << st.gain[0] << L" at 0.5 nit, " << st.gain[1]
                                       << L" at 100, " << st.gain[2] << L" at 1000; DIP " << Wide(st.measured);
                            }
                        }
                        notify::Post(failed ? notify::Sev::Warning : notify::Sev::Info, e.index, L"mhc", title,
                                     detail.str());
                    }
                }
            }
        } else if (hold && resolved && current != st.wantStamp) {
            st.waiting = hold;   // the want itself waits
            wait = true;
        }

        // Log a display's line when it changes (the checks repeat every few seconds).
        std::ostringstream line;
        line << st.state << " " << FmtHz(st.liveHz) << " Hz " << st.rateKey << " want=" << st.wantStamp
             << " active=" << st.activeStamp << (st.waiting.empty() ? "" : " waiting=") << st.waiting;
        if (g_lastLogged[e.index] != line.str()) {
            g_lastLogged[e.index] = line.str();
            std::cout << "[Refresh trim] monitor " << e.index << ": " << line.str() << " (" << st.detail << "; "
                      << reason << ")" << std::endl;
        }
        status.push_back(std::move(st));
    }

    if (failed) {
        const int step = (std::min)(g_failures, (int)(sizeof(kFailRetryMs) / sizeof(kFailRetryMs[0])) - 1);
        g_failures++;
        g_retryNotBefore = GetTickCount64() + kFailRetryMs[step];
        ArmRetry(kFailRetryMs[step]);
        if (g_failures >= kWarnAfterFailures && !g_warned && !g_exiting) {
            g_warned = true;
            notify::Post(notify::Sev::Warning, -1, L"mhc", L"Refresh trim could not be applied",
                         L"The HDR profile variant for this refresh rate could not be swapped in; retrying");
        }
    } else if (!wait) {
        g_failures = 0;
        g_retryNotBefore = 0;
        g_warned = false;
    }
    if (wait && !failed) ArmRetry(kWaitRetryMs);
    else if (rateUnknown && !failed) ArmRetry(kRateRetryMs);
    {
        std::lock_guard<std::mutex> lk(g_statusMutex);
        g_status = status;
    }
    if (swapped) {
        SaveSettings();   // the active variant: the next start re-associates this one
        // An association is where Windows' silent MHC drop bites: verify + sweep + Calibration Loader kicks after it.
        if (!g_exiting && g_gui.hwndMain) PostMessage(g_gui.hwndMain, WM_MHC_RELOAD_HARDWARE, kMhcReloadRefreshTrim, 0);
    }
    if ((swapped || changed) && !g_exiting) GuiNotifySettingsChanged(-1);
}
