#pragma once
// DesktopLUT - refresh_trim.h
// The per-refresh-rate grey trim (the owner's choice (a), 2026-10-23: refresh-keyed MHC profile variants).
//
// Some panels are brighter at lower refresh rates: the PA32UCXR in HDR at 47.952 Hz is +3.4 % at 0.5..10 nits,
// +2.6 % at 100, +1.0 % at 1000 (50 Hz: +3.0 .. +0.9 %; 48 = 47.952; 59.94 = 60). Luminance only, channel-independent,
// a function of the signal level, and NOT linear in frame period — so only measured rates get a trim.
//
// THE DATA: DLC measures it and records the RESPONSE in the display's DIP — <DLC folder>\dip_store.json, record
// "<display>:HDR", section refresh_response (DLC dip.py): per measured rate (its exact rational), the light at that rate
// over the light at 60 Hz at each level (keyed by the level's 60-Hz light), the run, the date, the identity of the
// panel and the conditions. A measured response, not a correction. DesktopLUT reads it through the DLC folder setting
// (the DLC tab's: [UI] DlcFolder, else found next to DesktopLUT) and DERIVES the trim at bake time: g = 1 / ratio,
// held below the lowest level, extended to the panel peak without crossing 1 (DeriveRefreshTrim). No DLC folder, no
// DIP, no section, the display's switch off (INI HDR_MHCRefreshTrim=0) = no trim, and every profile byte-identical
// to one baked without this feature.
//
// WHICH SECTION: the section's identity.hardware_id (the EDID vendor + product the measurement read off the display,
// e.g. AUS322A) must equal the live display's (the id part of its EdidId, else of its device path). When more than
// one live display has that id (twin panels), the section's identity.device_path must also equal the display's (the
// connector it was measured on). The DLC display name is not used: DLC keys DIPs by name, DesktopLUT keys displays by
// device path / EDID, and the hardware id the measurement recorded is what both sides hold. Several qualifying
// sections (the panel measured under two DLC names): the newest measured.
//
// WHICH RATE: the display target's vSyncFreq rational (QueryDisplayRefreshRate) matched to a measured rate within
// kRefreshTrimRateTolHz (the nearest; 48 and 47.952 are 0.048 Hz apart). A rate DLC found indistinguishable from
// another (same_as) uses that rate's levels — one curve, one stamp, one variant (48 = 47.952); one found equal to the
// reference (59.94 = 60) gets none, as does the reference itself and any rate not measured (never interpolated).
//
// THE BAKE: the HDR MHC profile's 1D LUT, last stage (mhc.h MHC2ProfileParams::refreshTrim, ComposeMhcScanout); the
// matrix is untouched, so the MHC stays the sole owner of the neutral axis. Part of the grayscale stage: the stage
// bypass leaves it out.
//
// THE VARIANTS: keyed by permutation and STAMPED with the trim (MHCSettings::permTrimStamp = RefreshTrimStamp, a
// hash of everything the trim bakes), like desktop gamma's SDR-white stamp — never a permutation bit. An off-stamp
// variant is taken from the display's trim stash (a switch back is a swap of a file already there) or regenerated.
// The swap is the permutation cache's (SwapMhcPermBits, g_mhcSwapMutex: files only, associated once at the swap) under
// a try-lock of g_mhcMaintenanceMutex, and each swap restarts the MHC transition burst (verify, sweep, Calibration
// Loader kicks at +2/+7/+22 s): an association is where Windows' MHC silent-drop bug bites.
//
// HOLDS: nothing changes during a calibration session, a live edit, an open MHC dialog or a pinned settings vector —
// the wanted trim is not even updated (a measurement must see the stack it started with). Retried afterwards.
//
// RE-READING THE DIP: the DLC folder scan (dlc_scan.h, off the GUI thread) is asked for on every WM_DISPLAYCHANGE (a
// refresh switch), the switch / RELOAD on the MHC tab, a layers.set refresh_trim, and by the DLC tab while it is shown;
// a scan whose dip_store.json changed resolves every display again (dlc::SetDipListener). One synchronous read at
// startup, so the first bakes already know the trim.

#include "types.h"
#include "dlc/dlc_data.h"

#include <cstdint>
#include <string>
#include <vector>

// A live rate is a measured one within this, and nearer to it than to any other (DLC refresh_mode RATE_TOL_HZ: CVT-RB
// "60" can be 59.997, a CRU timing's quantum ~0.001 Hz; neighbours are >= 0.048 Hz apart).
constexpr double kRefreshTrimRateTolHz = 0.01;

// ---- pure (tested) -------------------------------------------------------------------------------------------------

// A trim's identity: 16 lower-case hex digits, FNV-1a 64 over a bake-version tag and every value it bakes (rate key,
// peak, knots, as their bit patterns). What the variants are stamped with.
std::string RefreshTrimStamp(const MhcRefreshTrim& trim);

// The trim of one measured rate: ln g = -ln(ratio) per level (ascending, duplicates of a level dropped), extended to
// peakNits (> 0; else no cap). False (why) when the rate has no usable level or a ratio outside 0.5..2.
bool DeriveRefreshTrim(const dlc::DipRefreshRate& rate, double peakNits, MhcRefreshTrim& out, std::string* why = nullptr);

enum class RefreshTrimPick { Reference, Unmeasured, Trim, Invalid };
struct RefreshTrimChoice {
    RefreshTrimPick pick = RefreshTrimPick::Unmeasured;
    int matched = -1;      // section.rates index of the rate the live one is (-1: none)
    int curve = -1;        // the rate whose levels it uses (same_as followed)
    std::string why;
};
// What a display running at liveHz gets from a section.
RefreshTrimChoice ChooseRefreshTrim(const dlc::DipRefreshSection& section, double liveHz);

struct RefreshTrimLiveId {
    std::string hardwareId;     // EDID vendor + product, upper case
    std::wstring devicePath;
};
// The section (index) that describes `self` in `mode` ("HDR"), given every live display; -1 none (why).
int FindRefreshSection(const std::vector<dlc::DipRefreshSection>& sections, const std::string& mode,
                       const RefreshTrimLiveId& self, const std::vector<RefreshTrimLiveId>& live,
                       std::string* why = nullptr);

// One display's resolution: the trim it wants now and why. state: "off" (its switch) | "no_dlc" | "no_dip" |
// "no_section" | "invalid" (the section or the rate's levels unusable) | "reference" | "unmeasured" | "trim".
struct RefreshTrimResolution {
    MhcRefreshTrim want;        // active only in state "trim"
    std::string state, detail;
    int section = -1;           // the section used (-1 none)
};
RefreshTrimResolution ResolveRefreshTrim(bool enabled, bool haveFolder, bool dipFound,
                                         const std::vector<dlc::DipRefreshSection>& sections,
                                         const RefreshTrimLiveId& self, const std::vector<RefreshTrimLiveId>& live,
                                         double liveHz, double fallbackPeakNits);

// ---- runtime -------------------------------------------------------------------------------------------------------

// One live display, as of the last reconcile (thread-safe copy: state.get, the interface).
struct RefreshTrimStatus {
    int monitor = -1;
    bool enabled = true;                 // the display's switch
    double liveHz = 0.0;                 // the target's refresh now (0: unknown)
    uint32_t num = 0, den = 0;           // its rational
    std::string state, detail;           // RefreshTrimResolution's, or "no_rate" (the rate could not be read)
    std::string rateKey;                 // the trim's measured rate (state "trim")
    std::string wantStamp;               // the trim the rate wants ("" none)
    std::string activeStamp;             // what the HDR profile on screen carries ("" none, "?" unknown)
    bool installed = false;              // an HDR MHC profile of ours is installed (it carries the trim)
    bool applied = false;                // the profile on screen carries the wanted trim (or none is wanted)
    std::string waiting;                 // "calibration" | "mhc_dialog_open" | "settings_pinned" | "live_edit" |
                                         // "mhc_maintenance" | "retry_backoff" | "" (nothing waits)
    std::string recordKey, measured, runId;   // the DIP section in use
    int ageDays = -1;                    // the section's age (-1 unknown)
    bool stale = false;                  // older than the record's max_age_days (DLC's default 180)
    std::vector<std::string> rates;      // "47.952 Hz trim", "48 Hz = 47.952", "59.94 Hz = 60" (the section's)
    double gain[3] = { 1.0, 1.0, 1.0 };  // the wanted trim's g at 0.5 / 100 / 1000 nits
    std::wstring profileName;
};
std::vector<RefreshTrimStatus> GetRefreshTrimStatus();

// GUI thread, once the main window exists, before anything bakes an HDR profile: the DIP listener, one synchronous read
// of the DIP (the scan worker answers later), and a scan request.
void InitRefreshTrim();
// Any thread: resolve every display again on the GUI thread (coalesced, WM_REFRESH_TRIM).
void RequestRefreshTrim();
// GUI thread: ask the DLC folder scan to read the folder again (the DIP); a changed DIP resolves every display again.
void RequestRefreshTrimDipScan();
// GUI thread: resolve each live display's trim (its rate, its DIP section) and put the matching HDR variant on.
// Waits out the holds and the profile maintenance, backs off after a failed swap (REFRESH_TRIM_TIMER_ID); `why`
// "periodic" (the MHC verify tick) leaves a failed swap to that timer's backoff.
void ReconcileRefreshTrim(const char* why);
// GUI thread: a display's switch (HDR), persisted; reads the DIP again and reconciles. False: no such display.
bool SetRefreshTrimEnabled(int monitor, bool on);
// GUI thread, at exit after the engine stopped: every display back to its untrimmed variant (nothing follows the rate
// once DesktopLUT is gone, and untrimmed is right at the reference rate). Kept when the session ends.
void ReleaseRefreshTrimForExit(bool sessionEnding);
