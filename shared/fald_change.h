// DesktopLUT - shared/fald_change.h
// Change-driven execution of the FALD layer (2026-10-22, work guide core-speed): the pure, D3D-free decisions both hosts
// make before a run — the zones a present's dirty rects touch, and whether the run may take the PARTIAL path, where
// only those zones' statistics are recomputed and the GPU itself decides (shared/fald_shader.h g_faldDecideSource +
// DispatchIndirect / DrawInstancedIndirect) whether anything downstream has to run. Standalone: no Windows, no D3D.
//
// Why it is exact. A zone's statistic (drive, non-black flag; starfield S0's stat / bg) is a pure function of its own
// pixels [and the starfield plan]; a zone no rect touched has the same pixels as at the last run, so its statistic is
// unchanged by definition and need not be recomputed. The touched zones are recomputed by the SAME shader, group size and
// thread-to-pixel mapping as a full sweep (the zone's identity comes from a list instead of the group id), so their values
// are the full sweep's bit for bit; a changed value sets a flag the decide pass reads. No drive / flag changed -> the
// convolution, the gain and the round-1 statistic would reproduce the previous run's textures exactly (they are pure
// functions of the drive maps), so they are skipped and the previous textures stand; the round-1 statistic is then
// re-swept on the touched zones only (the corrected frame of an untouched zone is unchanged: same pixels, same fields);
// unchanged again -> the pixel pass is needed only where DWM re-composed the back buffer: the rects. Anything else
// (new content everywhere, a settings change, LED lag still settling, an unknown frame) takes the full path.
#pragma once

#include <stdint.h>
#include <vector>

struct FaldChangeRect { int left, top, right, bottom; };   // half-open, frame pixels

// The zones (linear index row * cols + col) whose pixels lie inside any of the rects (clipped to the lattice: origin +
// latW x latH, cols x rows zones, the shaders' ZoneOfPixel rounding). Deduplicated, ascending. `maxZones` caps the
// list: more touched zones than that returns false (take the full path: a long list costs more than it saves).
// Degenerate / empty rects touch nothing; a rect entirely outside the lattice touches nothing.
bool FaldTouchedZones(const FaldChangeRect* rects, int numRects, unsigned int originX, unsigned int originY, unsigned int latW,
                      unsigned int latH, unsigned int cols, unsigned int rows, unsigned int maxZones, std::vector<uint32_t>& out);

// The partial path's preconditions besides the rects, as the host knows them before the run.
struct FaldChangeInputs {
    bool stateComplete;        // the previous run was a full or partial run on THIS panel / size / mode whose textures
                               // all stand (false after a build, a swap, a mode change, an idle period, a dump)
    bool settingsSame;         // debug view, pedestal mode, starfield on/off + parameters, the core's mode: as at that run
    bool frameKnown;           // the intermediate's identity is known: the LUT / tone map pass is deterministic
                               // (lutPassKey != 0) and nothing but the rects changed it
    bool newContent;           // the present carries content outside the kick zones (information only: a kick-only
                               // present goes partial like any other small change)
    bool gainFused;            // passes 2b + 2c run as one dispatch (the three-pass fallback has a copy, which cannot be
                               // made indirect)
    unsigned int temporalMode; // LED lag (shared/fald_temporal.h): 0 off, 3 panel clock. The state's own movement is
                               // detected on the GPU (the clock pass compares its outputs with the previous run's and
                               // flags a change), so no settle bookkeeping is needed here
    unsigned int touchedZones; // the list's size (0 = no rect inside the lattice)
    unsigned int totalZones;
};
enum FaldChangePath : unsigned int {
    FALD_CHANGE_FULL = 0,      // every pass, as before
    FALD_CHANGE_PARTIAL = 1,   // the touched zones' statistics + the GPU-gated remainder
};
// FULL unless every precondition holds: the state complete and the settings the same, the frame known, the gain pass
// fused, a known LED-lag mode (0 or 3), and the list below the cap (touchedZones <= maxZones, > 0 is not
// required: no touched zone means nothing to recompute). A full-frame rect touches every zone and takes the full path
// by the cap.
FaldChangePath FaldChangeDecide(const FaldChangeInputs& in, unsigned int maxZones);
// The cap the hosts use: half the zones (a list of more costs more than it saves), at least 1.
unsigned int FaldChangeMaxZones(unsigned int totalZones);

// The indirect-argument slots the decide pass writes (shared/fald_shader.h g_faldDecideSource ARGS_*: 3 uints each, the
// pixel pass's draw 4), byte offsets into a FALD_ARGS_BYTES raw buffer the hosts create with the indirect-args flag.
enum FaldArgsSlot : unsigned int {
    FALD_ARGS_STAR_S1 = 0, FALD_ARGS_STAR_S2 = 16, FALD_ARGS_STAT0_FULL = 32, FALD_ARGS_STAT0_FULL_COMBINE = 48,
    FALD_ARGS_STAT0_PART = 64, FALD_ARGS_STAT0_PART_COMBINE = 80, FALD_ARGS_CONV0 = 96, FALD_ARGS_GAIN0 = 112,
    FALD_ARGS_STAT1_FULL = 128, FALD_ARGS_STAT1_FULL_COMBINE = 144, FALD_ARGS_STAT1_PART = 160, FALD_ARGS_STAT1_PART_COMBINE = 176,
    FALD_ARGS_CONV1 = 192, FALD_ARGS_GAIN1 = 208, FALD_ARGS_PIXEL = 224,
    FALD_ARGS_STAR_S0_FULL = 240, FALD_ARGS_STAR_S0_FULL_COMBINE = 256,  // S0 over the whole lattice (the frame changed everywhere)
    // the always-on core's lattice passes (shared/fald_core2.h): Z1 / Z2 after round 0's decide; the fine passes per round
    FALD_ARGS_CORE_LATTICE = 272, FALD_ARGS_CORE_FINE0 = 288, FALD_ARGS_CORE_FINE1 = 304,
    // the complements, so a partial present never does a thing twice: the pixel pass over the RECTS (the hook's draws: 1
    // instance exactly when the whole-frame draw of FALD_ARGS_PIXEL has 0), and S0 over the TOUCHED zones when the frame may
    // have changed everywhere (nothing when the whole-lattice sweep of FALD_ARGS_STAR_S0_FULL runs)
    FALD_ARGS_PIXEL_RECTS = 320, FALD_ARGS_STAR_S0_PART = 336, FALD_ARGS_STAR_S0_PART_COMBINE = 352
};
constexpr unsigned int FALD_ARGS_BYTES = 368;
// The slots that live in the LATE arguments buffer (below, "THE LATE ARGUMENTS"): the two statistic sweeps', the pixel
// pass's. The hosts dispatch / draw these from that buffer and everything else from the first.
inline bool FaldArgsLate(unsigned int slot) {
    return (slot >= FALD_ARGS_STAT0_FULL && slot <= FALD_ARGS_STAT0_PART_COMBINE) || (slot >= FALD_ARGS_STAT1_FULL && slot <= FALD_ARGS_STAT1_PART_COMBINE) ||
           slot == FALD_ARGS_PIXEL || slot == FALD_ARGS_PIXEL_RECTS;
}
// The change flags (u5, FALD_CHANGE_FLAGS uints, zeroed at build and by the last decide of a run): [0] / [1] = a drive, a
// zone flag or an LED-lag map of that round moved, [2] = the starfield's S0 moved, [3] = THE FRAME CHANGED EVERYWHERE —
// the caller's word that the pass which made the intermediate (the tone map with a dynamic peak: shared/peak_detect.h,
// the smoothing pass's draw arguments) is not the function it was at the previous run, so no zone's statistic can be
// taken as unchanged: a decide pass before any sweep (RunDecide 3) then orders S0 and round 0's sweep in full, and
// every later decide treats it as "moved". The hosts copy the caller's 4-byte flag into [3] (a GPU copy, no readback).
constexpr unsigned int FALD_CHANGE_FLAGS = 4;
// [0]'s bits: 1u = a drive / zone flag / LED-lag map of round 0 moved; the always-on core's (shared/fald_core2.h), each set
// by the pass that can see it and each ordering only what reads the value that moved:
//   2u  a zone's CORE STATISTICS (P, M, lnG) moved (the statistic sweep)        -> the core's zone passes Z1 / Z2
//   4u  a NODE PEAK moved (the statistic sweep)                                 -> the zone passes and both rounds' cap passes
//   8u  ALPHA_z, the one zone value the correction reads, moved (Z2 compares)   -> the round-1 sweep in full, the pixel pass
// and [1]'s: 1u = a drive / zone flag of round 1 moved. A round's CAP A is compared by its last pass too: round 0's moved
// -> the round-1 sweep in full; round 1's moved -> the pixel pass over the whole frame (no flag needed: see below).
// [2], the starfield's: 1u = a zone's S0 statistic moved (the S0 sweep) -> the plan passes S1 / S2; 2u = the PLAN the
// pixels read (S1's plan2, S2's plan) moved (they compare) -> round 0's sweep in full and everything after it. S0's
// statistic holds a zone's darkest pixel, its lit sum and the brightest pixel's position: it moves with almost any
// change, the plan of a lit desktop does not.
// The zone statistics move with almost any pixel (a mean and a geometric mean), alpha_z and the caps only when what they
// are made of really changes the correction: a caret in a lit document moves M and lnG and nothing the pixels read.
// THE LATE ARGUMENTS. The decide pass orders the two statistic sweeps and the pixel pass from the flags it can see; a
// pass after it that finds its own output moved RE-ORDERS what reads that output by storing the arguments itself
// (fald_shader.h FALD_ORDER_SWEEP_FULL / FALD_ORDER_PIXEL_FULL: S1 / S2 round 0's sweep, Z2 and round 0's last cap pass
// the round-1 sweep, round 1's last cap pass the pixel pass), so no decide stage follows it. Those slots — FaldArgsLate —
// live in a SECOND buffer of the same layout: a compare pass is itself dispatched from the first, so no dispatch ever
// writes the buffer its own arguments come from.
constexpr unsigned int FALD_CHANGE_CORE_MOVED = 2u;
constexpr unsigned int FALD_CHANGE_NODE_MOVED = 4u;
constexpr unsigned int FALD_CHANGE_ALPHA_MOVED = 8u;
constexpr unsigned int FALD_CHANGE_STAR_STAT_MOVED = 1u;   // of [2]
constexpr unsigned int FALD_CHANGE_STAR_PLAN_MOVED = 2u;   // of [2]
constexpr unsigned int FALD_CHANGE_FLAG_FRAME_BYTES = 12;   // byte offset of [3]
// CB word 79 (fald_shader.h runMode): the path, the conv pass's sub-offsets per thread, the list's length.
inline uint32_t FaldRunModeWord(bool partial, unsigned int convSo, unsigned int listLen) {
    return (partial ? 1u : 0u) | ((convSo & 0xFFu) << 8) | ((listLen > 0xFFFFu ? 0xFFFFu : listLen) << 16);
}
