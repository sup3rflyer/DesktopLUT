// DesktopLUT - shared/fald_core_mode.h
// The always-on FALD core's MODE TABLE and its DEFAULT (shared/fald_core2.h has the layer; this header has no D3D so
// src/types.h can carry the default in FaldSettings). KC5 (round 5, fald/principled d01e714, ported on fald/core-kc5,
// 2026-10-23) is C's fill with a brightening CAP away from knee-bound pixels plus alpha's level law a5; it is the first
// rule with BOTH measured outcomes — the colorimeter fill re-run (DLC results/fald_fill_gt_kc5_2026-10-23/RESULT_FILL6.md:
// KC5 keeps C's fill, RMS vs the target at neutral levels 1.77 % vs C 1.51 %, KC3 5.89 %) and the owner's live blind test
// (results/fald_principled_2026-10-22/RESULT_LIVE5.md: NO dark band in any of 12 clips, C in 5 of 12) — so KC5 IS what
// ships (mode 4).
// The earlier rounds' rules KC2c (mode 1) and KC3 (mode 2) and the reserved KC4 (mode 3) were A/B test modes reached through
// the INI key <mode>_FaldCoreModeTest and the pipe's runtime.fald_core_mode; both and the modes went 2026-11-12. Mode 0 =
// C, the core OFF: what runs when the core's shaders or textures are unavailable, and what the tests select to hold the
// layer to its references; no user surface reaches it. The numbers 0 and 4 keep their meaning in the hook's flag word and
// the status word; 1..3 are free (a host or DLL that sends one runs as C).
#pragma once
#include <cstdint>

// The core's flag bits (FaldCoreCB.coreFlags, b3 in the shaders). Bits 1 / 2 (the deficit, the soft leak floor) and 4 / 5
// were the removed rounds': never set.
constexpr uint32_t FALD_CORE2_ALPHA = 1u;              // the near-field brightening shrunk by the zone's alpha
constexpr uint32_t FALD_CORE2_FINE = 8u;               // the fine grid's node peaks (the cap's input)
constexpr uint32_t FALD_CORE2_CAP = 64u;               // the brightening CAP on the fine grid (fald_core2.h C1 / CX / CY)
constexpr uint32_t FALD_CORE2_ALPHA5 = 128u;           // alpha's level law a5 (the round-3 law below 0.5 nit, 1 above 2 nit)
constexpr uint32_t FALD_CORE2_KC5 = FALD_CORE2_ALPHA | FALD_CORE2_FINE | FALD_CORE2_CAP | FALD_CORE2_ALPHA5;   // = 201

// The modes (index = FaldSettings::coreMode): 0 = C (the core off), 4 = KC5 (THE DEFAULT).
constexpr uint32_t FALD_CORE_MODE_C = 0u, FALD_CORE_MODE_KC5 = 4u;
constexpr uint32_t FALD_CORE_MODE_LIMIT = 15u;         // the hook's flag word holds four bits (dwm_hook_config.h DWM_HOOK_FALD_CORE_MASK)
static_assert(FALD_CORE_MODE_KC5 <= FALD_CORE_MODE_LIMIT, "the hook's core-mode field (four bits) cannot carry this mode");

// THE DEFAULT: what a FaldSettings is born with (no INI key, no pipe verb sets anything else).
constexpr uint32_t FALD_CORE_MODE_DEFAULT = FALD_CORE_MODE_KC5;

// A mode this build runs: C or KC5; anything else runs as C.
inline bool FaldCoreModeKnown(uint32_t mode) { return mode == FALD_CORE_MODE_C || mode == FALD_CORE_MODE_KC5; }
inline uint32_t FaldCore2FlagsOfMode(uint32_t mode) { return mode == FALD_CORE_MODE_KC5 ? FALD_CORE2_KC5 : 0u; }
inline const char* FaldCoreModeName(uint32_t mode) { return mode == FALD_CORE_MODE_KC5 ? "KC5" : (mode == FALD_CORE_MODE_C ? "C" : "?"); }
