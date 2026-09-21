// DesktopLUT DWM Hook - hook_fald.h
// The FALD (mini-LED local dimming) context-dependence correction, running inside dwm.exe.
//
// PHASE ONE: the STATELESS CORE only — stat -> boost -> conv -> gain, twice (two inverse rounds),
// then the pixel pass. Deliberately absent, and staying in the overlay path (src/fald.cpp) until
// this one is proven on hardware:
//   * starfield balancing (S0-S2) and glow fill (G0-G4)
//   * the first-order temporal filter (pass 1b, modes 1/2) and its delay ring
//   * the panel clock (pass 1c, mode 3) and the settle hold
// src/fald.h documents each of those as bit-identical when off, so leaving them out is not a
// behaviour fork: with them off, this path must produce EXACTLY what the overlay path produces for
// the same input frame. That is the acceptance gate, and it is why there is no "hook variant" of
// any formula here — the HLSL in shared/fald_shader.h and the file reader in shared/fald_panel.h
// are the same code both paths run.
//
// Everything here runs on DWM's own device and immediate context, inside the Present path. Two
// consequences shape the code. A failure must degrade to an uncorrected frame rather than take DWM
// down, and unlike the rest of the hook this file does NOT throw: by the time the passes run, the
// LUT pass has already gone to the intermediate instead of the back buffer, so an exception would
// leave the back buffer unwritten — every fallible step is checked and reported by return value
// instead. And every binding made here must be cleared before returning (FaldUnbindAll): DWM reuses
// this context for its own rendering, where a stale SRV is a wrong texture and a stale UAV a GPU
// fault.
#pragma once

#include <dxgiformat.h>

#include "dwm_hook_config.h"
#include "fald_panel.h"

struct ID3D11Device;
struct ID3D11DeviceContext;
struct ID3D11ShaderResourceView;
struct ID3D11RenderTargetView;
struct ID3D11Texture2D;

// The binding ranges the layer touches on DWM's immediate context, as the HLSL declares them
// (t0..t24, u0..u1). hook_render.cpp clears these same ranges in its own cleanup paths — a slot
// this layer left bound is DWM's problem the moment the hook returns.
#define HOOK_FALD_SRV_SLOTS 25
#define HOOK_FALD_UAV_SLOTS 2

// Per-monitor GPU resources + the panel file they were built for. Owned by hook_fald.cpp, handed
// out by FaldAcquire and released wholesale by FaldReleaseAll (UninitializeStuff).
struct FaldMonitor;

// Compile the core's shaders (5 compute + the pixel pass + a fullscreen-triangle vertex shader) on
// DWM's device. Safe to call repeatedly; false leaves the layer permanently off for this attach.
// Called from InitializeStuff, so a compile failure costs the FALD layer and nothing else.
bool FaldInitShaders(ID3D11Device* dev, ID3D11DeviceContext* ctx);
void FaldReleaseShaders();
bool FaldShadersReady();

// Read the panel parameter files the host staged for this injection ("<left>_<top>.bin" and
// "<left>_<top>_hdr.bin" under <lutFolder>\fald). Called once at attach, beside AddLUTs: the
// staging directory is deleted right after injection, so there is no second chance and no polling.
// Returns the number of files parsed. Files that fail to parse are logged and skipped.
int FaldLoadPanelFiles(const char* lutFolder);

// True when a panel file was staged for this monitor position and mode. Cheap; no D3D.
bool FaldHasPanelFile(int left, int top, bool isHdr);

// The monitor's resources, built on first use for this (position, mode, frame size, format).
// Returns null when there is no panel file for it, when the file's transfer does not match the
// mode, when the lattice does not fit the frame, or when a resource could not be created — all of
// which are logged once and then latched, so a bad file costs one log line, not one per frame.
// `format` is the back buffer's, which the full-size intermediate below has to match.
FaldMonitor* FaldAcquire(int left, int top, bool isHdr, unsigned int width, unsigned int height,
                         DXGI_FORMAT format);

// The layer's own full-size intermediate. The hook's LUT/tonemap pass renders the WHOLE frame here
// instead of the back buffer (or, with neither configured, the back buffer is copied in), and
// FaldRun reads it and writes the back buffer. It belongs to the monitor rather than being one
// global: two FALD monitors of different sizes would otherwise recreate it on alternate frames.
ID3D11RenderTargetView* FaldIntermediateRTV(FaldMonitor* m);
ID3D11Texture2D* FaldIntermediateTexture(FaldMonitor* m);

// The layer's clean source (see FaldMonitor::cleanTex): the composed frame before any of our passes,
// kept current from DWM's dirty rects only. DWM re-composes nothing outside those rects — the back
// buffer there holds the previous frame's finished (corrected) output — so the layer must never read
// the back buffer wholesale, or it corrects its own output again every present.
// FaldUpdateClean copies this present's dirty rects in and returns true once the copy is PRIMED
// (a full-frame rect has landed since it went stale); until then the caller must not run the layer.
// FaldMarkStale is called for every present of a monitor the layer did NOT see (layer off, other
// mode): the rects of that present never reached the copy, so it has to be re-primed.
bool FaldUpdateClean(FaldMonitor* m, ID3D11Texture2D* backBuffer, const struct tagRECT* rects, int numRects);
void FaldMarkStale(int left, int top);
ID3D11Texture2D* FaldCleanTexture(FaldMonitor* m);
ID3D11ShaderResourceView* FaldCleanSRV(FaldMonitor* m);

// Live settings from the shared config (DwmHookSharedConfig::faldFlags), applied to the next run.
void FaldSetLiveSettings(FaldMonitor* m, unsigned int debugMode, int pedMode);

// Run the correction: read the frame left in this monitor's intermediate (full frame, FP16 scRGB,
// 1.0 = 80 nits) and write the corrected frame into `dstRTV` (the back buffer). Leaves nothing
// bound on DWM's context. Does not throw: a failure inside it would leave the back buffer
// unwritten, so every step that can fail is checked and the caller is told to fall back by the
// return value (false = copy the intermediate to the back buffer and carry on uncorrected).
bool FaldRun(FaldMonitor* m, ID3D11RenderTargetView* dstRTV);

// Microseconds the last FaldRun spent on the CPU side of the present path (QPC span around the
// dispatches; the GPU work is asynchronous). Logged periodically so the frame cost is a number
// rather than a guess — the first risk on the phase-one list.
double FaldLastRunMicros(const FaldMonitor* m);

// One-shot field dump for the acceptance gate: the next run writes drive / B_true / B_est / the
// input frame / the output frame into `dir` (same file names and layouts as the overlay path's
// runtime.fald_dump, so the two are diffed directly, and both against dlc/fald/gpuemu.py).
// ONE frame only — per-frame readback in DWM's present path is the stall recorded in
// HANDOFF_HAGS_FLIPQUEUE_2026-09-06.md. Triggered by a file, not by shared memory: the dump is a
// development act, and the trigger file names the output directory.
void FaldPollDumpRequest();

void FaldReleaseAll();
