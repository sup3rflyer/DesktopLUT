#pragma once
// The Windows tone map: what Windows tells games and apps an HDR display peaks at (types.h WindowsToneMap).
// DesktopLUT tone maps HDR itself. An app or Windows told the panel's real peak tone maps as well — a second roll-off
// at the top. Told 10000 nits (the top of PQ), they leave the highlights to DesktopLUT.
//
// Carrier: the display's HDR MHC profile when one is installed — its luminance tags, the variant swapped by
// MHCSettings::PERM_PEAK_MAX (the curves stay scaled to the panel's measured peak). Every profile association
// replaces whatever else told Windows a peak, so on such a display the profile is the only carrier that holds (the
// DisplayConfig override lost to it within seconds, and reported sRGB primaries meanwhile). A display without one
// gets the DisplayConfig override, with its EDID primaries, sent again after every transition that drops it — and
// sent back to the EDID's own values when it no longer should report 10000 (only where DesktopLUT sent it).

#include "types.h"

// Whether a display's Windows tone map reports 10000 nits now: Bypass always, Panel never, Auto while DesktopLUT's
// own tone map is meant to run on it (switched on, the live layers not suspended, the app not exiting). The INTENT,
// not the moment: an engine restart (a cube load) never flips it — only an engine that will not tone map counts as
// "off" (a hook the watchdog gave up on, an overlay hidden for a VRR-whitelisted game: the caller passes toneMapOn
// false then). Pure.
bool WindowsToneMapReportsMax(WindowsToneMap mode, bool toneMapOn, bool liveLayersSuspended, bool appExiting);

// INI spelling ("auto", "bypass", "panel"); anything else parses as Auto.
const wchar_t* WindowsToneMapKey(WindowsToneMap mode);
WindowsToneMap ParseWindowsToneMap(const wchar_t* text);

// Any thread: resolve every display again on the GUI thread (coalesced). `reapply`: Windows may have dropped the
// DisplayConfig override (a mode change, a wake, a driver reset, a start) — send it again.
void RequestWindowsToneMap(bool reapply);

// GUI thread: resolve every live display's Windows tone map (hdrMHC.reportPeakMax) and make Windows report it — the
// HDR profile's variant swap, else the DisplayConfig override. What has to wait (a calibration, a live edit, an MHC
// dialog, the profile maintenance) or failed is retried on WINDOWS_TONEMAP_TIMER_ID; `why` "periodic" (the MHC
// verify tick) leaves a failed swap to that timer's backoff.
void ReconcileWindowsToneMap(const char* why, bool reapply);

// GUI thread, at exit after the engine stopped: Auto displays go back to the panel's peak — nothing tone maps once
// DesktopLUT is gone. Kept when the session ends (sign-out / restart: DesktopLUT runs again from the next sign-in,
// and the swap back would only cost a profile switch at its start).
void ReleaseWindowsToneMapForExit(bool sessionEnding);

// GUI thread: what the display's EDID declares as its HDR peak (cached per device path) — what Windows reports for
// a display without a profile or an override. 0 = unknown.
struct DisplayInfo;
float EdidHdrPeakNits(const DisplayInfo& display);
