// DesktopLUT - dwm_inject.h
// DWM Hook DLL injection/uninjection

#pragma once

#include <cstdint>
#include <string>
#include <vector>
#include "../shared/dwm_hook_config.h"

struct DwmHookMonitorLUT {
    int left = 0, top = 0;           // Desktop position from MONITORINFO
    std::wstring sdrLutPath;          // SDR .cube path (empty = no SDR LUT)
    std::wstring hdrLutPath;          // HDR .cube path (empty = no HDR LUT)
    // FALD panel parameter files (*.bin). Staged beside the LUTs and read at attach; a later change
    // is delivered to the resident hook live (DeliverDwmHookFaldPanel), which also updates these in
    // the injected set. The layer's live switches travel over the shared config
    // (DwmHookSharedConfig::faldFlags). Empty = no FALD file for that mode.
    std::wstring sdrFaldPath;
    std::wstring hdrFaldPath;

    bool operator==(const DwmHookMonitorLUT& o) const {
        return left == o.left && top == o.top && sdrLutPath == o.sdrLutPath && hdrLutPath == o.hdrLutPath &&
               sdrFaldPath == o.sdrFaldPath && hdrFaldPath == o.hdrFaldPath;
    }
};

// The hook is resident AND was injected by this process with exactly this staging set (positions +
// cube / FALD paths). A start that finds this — e.g. the auto-restart after the overlay thread died in
// hook mode, where nothing ejected the hook — need not eject and re-inject a healthy hook: each cycle
// stalls dwm.exe on the detach, re-patches, and re-rolls twin routing (T2.10). Cubes are read at attach
// only, so a changed cube at the same path still needs a Stop (eject) + Start; a FALD panel file is
// delivered live instead (DeliverDwmHookFaldPanel), and the set then names the file the hook holds.
bool DwmHookResidentWith(const std::vector<DwmHookMonitorLUT>& monitors);
// The same entries in any order (one entry per display position): a live panel delivery appends the entry of a display
// the injection did not stage, where DesiredDwmHookStagingSet lists it in display order. Pure (tested).
bool DwmHookStagingSetsEqual(const std::vector<DwmHookMonitorLUT>& a, const std::vector<DwmHookMonitorLUT>& b);
// The staging set of the last successful injection (empty, `*valid` false, when none describes the hook).
std::vector<DwmHookMonitorLUT> DwmHookInjectedSet(bool* valid);

// Check if the DWM hook is active via named event (lightweight, no elevation needed).
// The injected DLL creates Global\DesktopLUT_DwmHook_Active on attach.
bool IsDwmHookActive();

// Inject DwmHook.dll into dwm.exe with the given LUT configuration.
// Stages LUT files to %SYSTEMROOT%\Temp\DesktopLUT_luts\, copies DLL,
// elevates to SYSTEM via lsass token, and injects via CreateRemoteThread.
// Returns empty string on success, error message on failure.
// stagingWarning: LUT files that could not be read are skipped (the rest is injected) and listed here.
std::wstring InjectDwmHook(const std::vector<DwmHookMonitorLUT>& monitors, std::wstring* stagingWarning = nullptr);

// Uninject DwmHook.dll from all dwm.exe processes via FreeLibrary.
// Returns empty string on success, error message on failure.
// keepOwnSession: the session is ending (WM_ENDSESSION) — leave the hook in this session's dwm.exe,
// which ends with it, and eject only other sessions' copies.
std::wstring UninjectDwmHook(bool keepOwnSession = false);

// --- Shared memory IPC (live parameter updates to hook without re-injection) ---

// Create shared memory mapping. Called before injection so DLL can open it in DLL_PROCESS_ATTACH.
bool CreateDwmHookSharedMemory();

// Update shared memory with current tonemap params. Called on GUI changes and WM_DISPLAYCHANGE.
void UpdateDwmHookSharedConfig();

// Close shared memory mapping. Called on uninject/stop.
void CloseDwmHookSharedMemory();

// --- Live edit preview through the hook (DwmHookMhcPreview; src/mhc_preview.h) ---
// Writes the preview block for monitor `monitorIndex` (its desktop position is the key) and publishes its generation in
// the config; the DLL applies it from its next present. Returns the generation (0 = not written: no shared memory, or
// no such monitor). `lut` = three channels of `lutSize` entries.
uint32_t PublishDwmHookMhcPreview(int monitorIndex, bool isHdr, const float applied[9], int lutSize,
                                  const std::vector<float> lut[3]);
// No preview: generation 0 in the config. True when a preview was published (a recomposition is then requested).
bool ClearDwmHookMhcPreview();
// The generation the resident DLL last acknowledged (0 = none: no block, or a DLL that predates the preview).
uint32_t DwmHookMhcPreviewAck();
// The generation the config carries now (0 = none). A re-created shared memory re-publishes the wanted block under a
// new one, so this can move without a publish.
uint32_t DwmHookMhcPreviewGen();

// --- FALD panel file live delivery (dwm_hook_config.h DwmHookFaldPanelBlob) ---
// A panel file change reaches the resident hook without a re-injection: the layer keeps running on the old file until
// the new one is built and switches between two presents (re-injecting blinked it off for ~1 s per switch).
// Hand the file at `path` to the resident hook for the display at (left, top), mode `hdr`. The file is read and
// validated here with the DLL's parser (transfer vs mode; lattice and zone sweep vs the display, which must be in the
// DXGI list), copied into a new named section, and its generation (`*gen`) published in the shared config; the
// injected set names it at once (the live-layer reconcile must not re-inject for the path change) and is taken back
// if the hook's verdict says otherwise (CheckDwmHookFaldPanelVerdicts). True = sent. False (`why` says why): the hook
// cannot take it (not resident, an older DLL, no tuning tail, unknown staging set) or the file is refused — the
// caller then re-injects as before, which also gives a refused file exactly the old outcome.
// GUI thread; takes g_dwmInjectMutex, then g_monitorSettingsMutex (never call it holding the latter).
bool DeliverDwmHookFaldPanel(int left, int top, bool hdr, const std::wstring& path, std::wstring* why = nullptr,
                             uint32_t* gen = nullptr);
// The display's FALD path for `hdr` was cleared: the file the hook holds there is inert (the layer is off for want of
// a path, DwmHookSharedConfig::faldFlags) and is no longer watched for re-exports. True when a resident hook took
// note (no re-injection needed); false = no hook to tell.
bool ForgetDwmHookFaldPanel(int left, int top, bool hdr);
// The resident DLL takes live deliveries (DWM_HOOK_FALD_PANEL_LIVE_EVENT exists).
bool DwmHookFaldPanelLiveSupported();

// What the GUI is told about a delivery (status line / notification).
struct DwmHookFaldPanelNotice {
    enum class Kind { Loaded, LoadedIdle, Failed } kind = Kind::Loaded;   // LoadedIdle: held, the layer is not running
    int left = 0, top = 0;                                                //   there now (applies when it runs)
    bool hdr = false;
    uint32_t gen = 0;
    std::wstring text;                                                    // Failed: what to tell the user
};
// GUI thread: fold the hook's answers on outstanding deliveries in. Applied / held -> Loaded / LoadedIdle. Refused,
// could not be built, or no answer within a few hook-watchdog ticks (`watchdogTick`: one of them; counted only while
// the display is on and attached) -> Failed: the injected set and state.get name what the hook actually holds, and a
// fallback re-injection is owed — once per failed file stamp (DwmHookFaldPanelFallbackOwed / Take...).
// `wantRecompose`: a watchdog tick found a delivery still unanswered — ask DWM for a composition (a static desktop
// presents nothing, so the DLL never sees the new generation).
void CheckDwmHookFaldPanelVerdicts(bool watchdogTick, std::vector<DwmHookFaldPanelNotice>* notices, bool* wantRecompose);
bool DwmHookFaldPanelVerdictPending();   // a delivery is still unanswered
bool DwmHookFaldPanelFallbackOwed();     // a failed delivery owes the old path's re-injection
bool TakeDwmHookFaldPanelFallback();     // true (and cleared) when one is owed: the caller re-injects now
// Re-export poll (the overlay path's stamp poll): on the hook watchdog's healthy tick, the files the hook was given are
// stat'ed and read on a WORKER thread (never under g_dwmInjectMutex, never on the GUI thread: an unreachable share
// would stall both); the results come back as `doneMsg` (lParam) to `notifyHwnd`, and FinishDwmHookFaldPanelPoll
// (GUI thread) delivers what changed: a new content only (same bytes, touched: nothing), a file being written is
// retried (never recorded as bad), a refused one is not re-read until it changes again and is reported once if it
// stays. False = nothing to poll, or a poll still in flight.
bool StartDwmHookFaldPanelPoll(void* notifyHwnd, unsigned int doneMsg);
void FinishDwmHookFaldPanelPoll(intptr_t result, std::vector<DwmHookFaldPanelNotice>* notices, bool* delivered);
void DiscardDwmHookFaldPanelPoll(intptr_t result);   // frees a result without acting on it (shutdown)
// Per (position, mode), for state.get: what the hook HOLDS (its verdicts) and what was SENT last.
struct DwmHookFaldPanelState {
    int left = 0, top = 0;
    bool hdr = false;
    std::wstring heldPath;   // the file the hook holds there ("" = none)
    uint32_t heldGen = 0;    // its generation (0 = staged at injection)
    std::wstring sentPath;   // the last file delivered live ("" = none since the injection)
    uint32_t sentGen = 0;    // its generation: a client that sent one waits for status "applied" on it
    std::wstring wantPath;   // the file the host wants there ("" = path cleared)
    std::string status;      // staged | delivered | ingested | applied | refused | build_failed | no_ack
};
std::vector<DwmHookFaldPanelState> DwmHookFaldPanels();

// Invalidate cached DXGI monitor info (call on WM_DISPLAYCHANGE).
void InvalidateDxgiMonitorCache();

// True when at least two monitors are indistinguishable to the hook (same size + bit depth):
// the only rigs where the identity beacon has anything to tell apart.
bool DwmHookHasTwinMonitors();

// --- Twin-panel routing (25H2 first-present order-match) ---------------------
// The hook DLL persists its overlay-context -> monitor assignment in
// DWM_HOOK_ROUTING_FILE_W (see dwm_hook_config.h). The host reads it for state.get
// and rewrites it for hook.set_routing; the DLL honours the rewritten pins on its
// next injection of the same dwm.exe.
struct DwmHookRoutingMon { int left = 0, top = 0, width = 0, height = 0, bpc = 0; };
struct DwmHookRoutingEntry {
    std::string ctx;      // hex pointer, lower-case, no 0x (as the DLL writes it)
    int left = 0, top = 0;
    std::string method;   // unique|bpc|scan|pinned|order|legacy|provisional|replaced|beacon|unknown
};
struct DwmHookRouting {
    bool present = false;          // a routing file exists and parsed
    std::string session;           // "<pid>-<createHigh>-<createLow>" of the dwm.exe that wrote it
    bool stale = false;            // that dwm.exe is no longer running (DWM restarted since)
    bool confirmed = false;        // a client verified the assignment through a meter
    std::vector<DwmHookRoutingMon> monitors;
    std::vector<DwmHookRoutingEntry> entries;
};
DwmHookRouting ReadDwmHookRouting();
bool WriteDwmHookRouting(const DwmHookRouting& r);   // rewrites the file (keeps the DLL's session key)
bool ClearDwmHookRouting();                          // deletes it — the next injection re-rolls
// Swap the assignment of the monitor at (left, top) with its single indistinguishable twin
// (same size + bpc): every context recorded at either position moves to the other one.
// Returns an error message (empty on success). The caller re-injects.
std::wstring SwapDwmHookRouting(DwmHookRouting& r, int left, int top);

