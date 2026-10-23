// DesktopLUT - shared/fald_core_mode.h
// The always-on FALD core's MODE TABLE and its DEFAULT (shared/fald_core2.h has the layer; this header has no D3D so
// src/types.h can carry the default in FaldSettings). Branch fald/core-kc, 2026-10-23: the owner's two live blind tests
// (DLC results/fald_principled_2026-10-22/RESULT_LIVE.md and the coordinator's 2026-10-23 note: FALD off / C / KC3 — the
// flicker is the panel's own, C's dark-band artefact in 5 of 12 clips, KC3's in none, real clips identical) made KC3 the
// default rule; C (mode 0) and KC2c (mode 1) stay reachable through the switch for A/B checks (the pipe
// runtime.fald_core_mode, the INI key <mode>_FaldCoreModeTest; no GUI option).
#pragma once
#include <cstdint>

// The core's flag bits (FaldCoreCB.coreFlags, b3 in the shaders).
constexpr uint32_t FALD_CORE2_ALPHA = 1u;              // the near-field brightening shrunk by the zone's alpha
constexpr uint32_t FALD_CORE2_DEFICIT = 2u;            // the soft knee's spatial edge removed (the deficit D)
constexpr uint32_t FALD_CORE2_SOFT = 4u;               // reserved (KC2's soft leak floor), never set by the hosts
constexpr uint32_t FALD_CORE2_FINE = 8u;               // ROUND 3: the fine-grid (sliding-window) deficit, no zone IIR
constexpr uint32_t FALD_CORE2_KC2C = FALD_CORE2_ALPHA | FALD_CORE2_DEFICIT;
constexpr uint32_t FALD_CORE2_KC3 = FALD_CORE2_ALPHA | FALD_CORE2_DEFICIT | FALD_CORE2_FINE;

// The mode table: index = FaldSettings::coreMode. 0 = C (the core off: the 2026-10-22 layer bit for bit), 1 = KC2c (round
// 2), 2 = KC3 (round 3). A LATER ROUND (KC4 = mode 3) is one more entry here, its flag bit above, and its passes in
// fald_core2.cpp (RunZone / RunDeficit dispatch on the flags): the hosts, the IPC, the INI, the status and the hook's flag
// word are mode-agnostic up to FALD_CORE_MODE_LIMIT.
struct FaldCoreModeDef { uint32_t flags; const char* name; };
constexpr FaldCoreModeDef FALD_CORE_MODES[] = { { 0u, "C" }, { FALD_CORE2_KC2C, "KC2c" }, { FALD_CORE2_KC3, "KC3" } };
constexpr uint32_t FALD_CORE_MODE_COUNT = (uint32_t)(sizeof(FALD_CORE_MODES) / sizeof(FALD_CORE_MODES[0]));
constexpr uint32_t FALD_CORE_MODE_C = 0u, FALD_CORE_MODE_KC2C = 1u, FALD_CORE_MODE_KC3 = 2u, FALD_CORE_MODE_MAX = FALD_CORE_MODE_COUNT - 1u;
constexpr uint32_t FALD_CORE_MODE_LIMIT = 3u;          // the hook's flag word holds two bits (dwm_hook_config.h DWM_HOOK_FALD_CORE_MASK)
static_assert(FALD_CORE_MODE_MAX <= FALD_CORE_MODE_LIMIT, "the hook's core-mode field (two bits) cannot carry this many modes");

// THE DEFAULT (the one line to flip): what a FaldSettings is born with and what an INI without the key loads.
constexpr uint32_t FALD_CORE_MODE_DEFAULT = FALD_CORE_MODE_KC3;
static_assert(FALD_CORE_MODE_DEFAULT <= FALD_CORE_MODE_MAX, "the default must be a mode of the table");

inline uint32_t FaldCore2FlagsOfMode(uint32_t mode) { return mode < FALD_CORE_MODE_COUNT ? FALD_CORE_MODES[mode].flags : 0u; }
inline const char* FaldCoreModeName(uint32_t mode) { return mode < FALD_CORE_MODE_COUNT ? FALD_CORE_MODES[mode].name : "?"; }
// The core has temporal state (the zone IIRs): its fields move run to run whatever the frame did — the change-driven path
// is not exact with it and the hosts take the full path (shared/fald_change.h FaldChangeInputs::coreStateful).
inline bool FaldCore2Stateful(uint32_t flags) { return (flags & (FALD_CORE2_ALPHA | FALD_CORE2_DEFICIT)) != 0u && (flags & FALD_CORE2_FINE) == 0u; }
