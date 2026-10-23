// DesktopLUT - shared/fald_core_mode.h
// The always-on FALD core's MODE TABLE and its DEFAULT (shared/fald_core2.h has the layer; this header has no D3D so
// src/types.h can carry the default in FaldSettings). Branch fald/core-kc, 2026-10-23. The owner's two live blind tests
// (DLC results/fald_principled_2026-10-22/RESULT_LIVE.md; FALD off / C / KC3: the flicker is the panel's own, C's dark-band
// artefact in 5 of 12 clips, KC3's in none, real clips identical) made KC3 the default for a few hours; the colorimeter FILL
// test the same day (DLC results/fald_fill_gt_2026-10-23: RMS vs the target at neutral levels C 1.41 %, KC3 4.57 %, native
// 5.20 %) showed KC3's alpha removes real correction, so THE DEFAULT IS C again — byte-identical to main / core-speed — and
// KC3 / KC2c are test modes for A/B checks (the pipe runtime.fald_core_mode, the INI key <mode>_FaldCoreModeTest; no GUI
// option). KC3's ring removal is still wanted: KC5 (C's fill + KC3's deficit) is being designed on fald/principled and lands
// here as MODE 4 (mode 3 stays KC4's); both are reserved below so the numbering is fixed before their port deltas arrive.
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
// 2), 2 = KC3 (round 3), 3 = KC4 (RESERVED: the round-4 tweak, if it comes), 4 = KC5 (RESERVED: C's fill + KC3's deficit, the
// round-5 port delta). A round lands as: its flag bit above, its entry's flags here (reserved -> false), its passes in
// fald_core2.cpp (RunZone / RunDeficit dispatch on the flags), and — if it has temporal state — FaldCore2Stateful saying so
// (the change-driven path then takes the full path); a stateless round must compare its sweep statistics in the stat pass
// the way KC3's are (changeFlags[0] bit 2u) for the partial path to stay exact. The hosts, the IPC, the INI, the status and
// the hook's flag word are mode-agnostic up to FALD_CORE_MODE_LIMIT; a reserved mode is refused by the IPC / the INI and
// runs as C in a host that meets it.
struct FaldCoreModeDef { uint32_t flags; const char* name; bool reserved; };
constexpr FaldCoreModeDef FALD_CORE_MODES[] = { { 0u, "C", false }, { FALD_CORE2_KC2C, "KC2c", false }, { FALD_CORE2_KC3, "KC3", false },
                                                { 0u, "KC4 (reserved)", true }, { 0u, "KC5 (reserved)", true } };
constexpr uint32_t FALD_CORE_MODE_COUNT = (uint32_t)(sizeof(FALD_CORE_MODES) / sizeof(FALD_CORE_MODES[0]));
constexpr uint32_t FALD_CORE_MODE_C = 0u, FALD_CORE_MODE_KC2C = 1u, FALD_CORE_MODE_KC3 = 2u, FALD_CORE_MODE_KC4 = 3u, FALD_CORE_MODE_KC5 = 4u;
constexpr uint32_t FALD_CORE_MODE_MAX = FALD_CORE_MODE_COUNT - 1u;
constexpr uint32_t FALD_CORE_MODE_LIMIT = 15u;         // the hook's flag word holds four bits (dwm_hook_config.h DWM_HOOK_FALD_CORE_MASK)
static_assert(FALD_CORE_MODE_MAX <= FALD_CORE_MODE_LIMIT, "the hook's core-mode field (four bits) cannot carry this many modes");

// THE DEFAULT (the one line to flip): what a FaldSettings is born with and what an INI without the key loads. C since the
// 2026-10-23 fill test (above); KC3 was the default from ~09:50 to ~13:00 that day and never shipped.
constexpr uint32_t FALD_CORE_MODE_DEFAULT = FALD_CORE_MODE_C;
static_assert(FALD_CORE_MODE_DEFAULT <= FALD_CORE_MODE_MAX && !FALD_CORE_MODES[FALD_CORE_MODE_DEFAULT].reserved, "the default must be a mode of the table");

// A mode the table defines (not reserved): what the IPC and the INI accept; anything else runs as C.
inline bool FaldCoreModeKnown(uint32_t mode) { return mode < FALD_CORE_MODE_COUNT && !FALD_CORE_MODES[mode].reserved; }
inline uint32_t FaldCore2FlagsOfMode(uint32_t mode) { return FaldCoreModeKnown(mode) ? FALD_CORE_MODES[mode].flags : 0u; }
inline const char* FaldCoreModeName(uint32_t mode) { return mode < FALD_CORE_MODE_COUNT ? FALD_CORE_MODES[mode].name : "?"; }
// The core has temporal state (the zone IIRs): its fields move run to run whatever the frame did — the change-driven path
// is not exact with it and the hosts take the full path (shared/fald_change.h FaldChangeInputs::coreStateful).
inline bool FaldCore2Stateful(uint32_t flags) { return (flags & (FALD_CORE2_ALPHA | FALD_CORE2_DEFICIT)) != 0u && (flags & FALD_CORE2_FINE) == 0u; }
