// DesktopLUT - shared/fald_fade.h
// The frozen fade stack F12B_V8d (FREEZE 2026-10-21: ASF10 + T11_TMTC of 2026-10-19 + the line fixes A — the CL closing on
// the zero-padded 1/8-res map — and B16 — the lit floor's sigma-16 Gaussian — of 2026-10-20 + round 12b's halo fix V8d —
// the floor-valley gate on the flank term; DLC results/fald_nearfield_2026-10-17/r12a/PORT_DELTA.md, r12b/PORT_DELTA.md)
// — resources, state and passes, ONE
// implementation for the overlay (src/fald.cpp) and the DWM hook (dwm_hook/hook_fald.cpp), as shared/fald_temporal.cpp is
// for LED lag. The HLSL and the rule: shared/fald_fade_shader.h (header comment); the float32 twin: DLC
// dlc/fald/fadeemu.py; the brief: DLC results/fald_nearfield_2026-10-17/r11/PORT_PLAN.md.
//
// The setting (FaldSettings::flankFade, hook faldFlags bit 8) reaches the shaders as FaldCB word 75 `fadeFlags` bit 0.
// OFF = none of this exists (no resources, no dispatch) and the layer is today's bit for bit (tests/test_fald_fade.cpp).
// ON, per run of the layer:
//   1. FaldFadeRunFrame once per CONTENT frame (the caller's newContent; or a settings change, without a T step): the
//      fade field, the lit floor's 1/8-res opening, the reach step map, the T state step (stepT) and s - f_q;
//   2. FaldFadeRunReach after each inverse round's RunGain (round 0, round 1): the knee-cap fraction of that round's
//      Correct -> reach_r. A run of the layer on the SAME input frame as the last one (no new content, LED lag off, the
//      same settings: the caller's key, FaldFadeReachCurrent) skips both rounds' reach — the fields, and so reach, are a
//      pure function of the frame then (DLC results/fald_fade_tiers_2026-10-20: Z_reach_skip, bit for bit);
//   3. the round-1 statistic and the pixel pass bind FaldFadePostViews(...) at t20..t23 and apply FadePost, which forms
//      fade_T and the lit floor F per pixel (fald_shader.h).
// T state (s, the previous frame's 1/8- and 1/4-res maps and windowed spectrum) persists across content frames; every
// "no history" point (FaldFadeReset) makes the next content frame start at s = f_q (fade_T = fade): a resource rebuild
// (size, panel file — the hook's live swap included —, device), FaldLayerIdle / FaldTemporalIdle (layer off, mode change,
// the hook's unprimed copy, a resume gap), a re-inject (a fresh DLL has none). The advance test is on the GPU (no readback).
// The GPU time of each content frame's passes (RunFrame + both RunReach) is measured with timestamp queries that are read
// a few frames later without a flush or a wait (FaldFadeCollectTiming): the safety governor's input (fald_fade_governor.h).
// Standalone: D3D11 only, no project headers (compiled into DesktopLUT.exe, DwmHook.dll and the tests).
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string>
#include <vector>

struct ID3D11Device;
struct ID3D11DeviceContext;
struct ID3D11Buffer;
struct ID3D11ShaderResourceView;
struct ID3D11SamplerState;
struct ID3D11Texture2D;

// FaldCB word 75 (fald_shader.h `fadeFlags`)
constexpr uint32_t FALD_FADE_FLAG_FLANK = 1u;    // bit 0: the flank-fade stack (F12B_V8d)
constexpr uint32_t FALD_FADE_FLAG_LOWEXT = 2u;   // bit 1: item L (the 1-3-nit extension) — RESERVED, never set (round 12)

// The FALD common state the fade passes that read the frame / the round's fields need (the caller's BindCommon views:
// b0 FaldCB, t0..t19, s0; FaldFadeRunReach also needs t5 / t6 = the round's B_true / B_est and t9 = its smoothed gain).
struct FaldFadeCommon {
    ID3D11Buffer* faldCB = nullptr;
    ID3D11ShaderResourceView* srv[20] = {};
    ID3D11SamplerState* sampler = nullptr;
};

// The fade passes ship as BYTECODE (shared/fald_fade_bytecode.h, generated from fald_shader.h's common source +
// fald_fade_shader.h with D3DCompile cs_5_0 + D3DCOMPILE_IEEE_STRICTNESS — the twin emulates that operation order): no
// shader compiler runs in the hook's present or the overlay's render thread (a compile of the passes took 0.4-0.55 s).
// tests/test_fald_fade.cpp fails when the embedded bytecode is not a fresh compile of the current sources, and writes
// the header (FALD_TEST_FADE_WRITE_BYTECODE).
//
// Create the passes from the bytecode (process-global; once per device), at most `maxCreates` per call (0 = all), then —
// with a context — warm each once (a dispatch over an empty domain with nothing bound: the driver finishes its compile of
// the pass there, not in the first content frame), `maxCreates` per call as well: the callers pace it over presents and
// run the layer WITHOUT the fade (today's layer) until Ready. Failed = unavailable for this attach (`err` gets the first
// failure); the callers then never set fadeFlags.
enum class FaldFadeStatus { Ready, Pending, Failed };
FaldFadeStatus FaldFadeShadersStep(ID3D11Device* dev, ID3D11DeviceContext* warmCtx, unsigned int maxCreates, std::string* err);
bool FaldFadeInitShaders(ID3D11Device* dev, std::string* err);   // = FaldFadeShadersStep(dev, nullptr, 0, err) == Ready
void FaldFadeReleaseShaders();
bool FaldFadeShadersReady();
// The passes' sources and bytecode (tests, the bytecode generator): k < FaldFadeKernelCount().
unsigned int FaldFadeKernelCount();
const char* FaldFadeKernelName(unsigned int k);
bool FaldFadeCompileKernel(unsigned int k, std::vector<unsigned char>& out, std::string* err);   // from the sources, now
bool FaldFadeEmbeddedBytecode(unsigned int k, const void** data, size_t* bytes);                // what ships

struct FaldFade;
FaldFade* FaldFadeCreate();                       // nothing allocated on the GPU yet
void FaldFadeDestroy(FaldFade* f);                // releases everything (null ok)
// (Re)build for a frame of width x height on `dev` (a no-op when already built for it). Paced: each call creates at most
// `maxCreates` resources and `maxBytes` of them (0 = no limit; at least one per call) — ~120 resources, ~480 MB at 4K —
// Pending until the last one exists; the caller runs the layer without the fade meanwhile. Failed = a resource could not
// be created (everything released; the caller runs the layer without the fade). A build resets T (no history).
// `touch` (optional: the context the layer runs on) clears each new texture to 0 as it is created, so the driver's
// first-use work on the memory is paced with the creation instead of landing in the first content frame.
FaldFadeStatus FaldFadeEnsureStep(FaldFade* f, ID3D11Device* dev, unsigned int width, unsigned int height,
                                  unsigned int maxCreates, unsigned long long maxBytes, ID3D11DeviceContext* touch = nullptr);
bool FaldFadeEnsure(FaldFade* f, ID3D11Device* dev, unsigned int width, unsigned int height);   // all at once
// The pace the overlay and the hook use (per present / per run of the layer)
constexpr unsigned int FALD_FADE_SHADERS_PER_RUN = 4;
constexpr unsigned int FALD_FADE_RESOURCES_PER_RUN = 12;
constexpr unsigned long long FALD_FADE_BYTES_PER_RUN = 64ull << 20;
void FaldFadeRelease(FaldFade* f);                // GPU memory back (the next Ensure rebuilds; T reset)
bool FaldFadeBuilt(const FaldFade* f);
void FaldFadeReset(FaldFade* f);                  // T: the next content frame has no history (s = f_q)
unsigned long long FaldFadeVramBytes(const FaldFade* f);   // the textures' size (log / tier plan)
// The cached frame part (fade, the floor's opening, the reach step map, s - f_q) is valid for the frame it was computed
// from: false after a build / release / reset, or when the caller invalidates it (a setting that changes the frame the
// layer works on, e.g. starfield).
bool FaldFadeFrameValid(const FaldFade* f);
void FaldFadeInvalidateFrame(FaldFade* f);

// Once per content frame (see the header): stepT = advance the T state (new content); false = re-form the frame part from
// the kept state with this frame's fade (a re-run after a settings change). Binds the common state itself, leaves nothing
// bound on b0..b2 / t0..t35 / u0..u7 / s0. Invalidates both rounds' reach. A stepped frame is timed (FaldFadeCollectTiming).
void FaldFadeRunFrame(FaldFade* f, ID3D11DeviceContext* ctx, const FaldFadeCommon& common, bool stepT);
// After round r's RunGain: reach_r from that round's fields (common.srv[5] / [6] / [9]); `key` = the caller's identity
// of the input frame + the settings the fields depend on (0 = unknown: never reused). Leaves nothing bound.
void FaldFadeRunReach(FaldFade* f, ID3D11DeviceContext* ctx, const FaldFadeCommon& common, unsigned int round,
                      unsigned long long key);
// Both rounds' reach were computed for `key` (non-zero) since the frame part last changed: the caller may skip them.
bool FaldFadeReachCurrent(const FaldFade* f, unsigned long long key);
// The views FadePost reads (fald_shader.h t20..t23): fade, the floor's 1/8-res opening, reach of `round`, s - f_q.
void FaldFadePostViews(const FaldFade* f, unsigned int round, ID3D11ShaderResourceView* out[4]);
constexpr unsigned int FALD_FADE_POST_SLOT = 20;   // the first of them
constexpr unsigned int FALD_FADE_POST_VIEWS = 4;

// The GPU time of finished content frames (RunFrame stepped + both rounds' reach), microseconds, oldest first: at most
// `maxOut`; queries the GPU has not finished are left for a later call (GetData with DONOTFLUSH: never a flush, never a
// wait). A frame whose timestamps were disjoint is dropped.
unsigned int FaldFadeCollectTiming(FaldFade* f, ID3D11DeviceContext* ctx, double* outUs, unsigned int maxOut);

// Test / dump access (tests/test_fald_fade.cpp, the dump): the fields of the last run and the T flags buffer. fade_T and
// F exist only per pixel inside FadePost; FaldFadeDebugMaterialize writes them as full-res maps (created on its first
// call: a dump or a test, never a present of its own) with the passes that formed them before — FadeUp + TOUT, FadeUp +
// exp — from the kept 1/4- and 1/8-res maps, bit for bit FadePost's values.
#define FALD_FADE_HAS_DEBUG_MATERIALIZE 1
void FaldFadeDebugMaterialize(FaldFade* f, ID3D11DeviceContext* ctx);
struct FaldFadeDebug {
    ID3D11Texture2D* mi = nullptr;      // full res: the max channel the fade was formed from
    ID3D11Texture2D* fade = nullptr;    // full res: the instantaneous ASF9o field of the last content frame
    ID3D11Texture2D* fadeT = nullptr;   // full res: fade_T (FaldFadeDebugMaterialize; null before)
    ID3D11Texture2D* floorF = nullptr;  // full res: F (FaldFadeDebugMaterialize; null before)
    ID3D11Texture2D* reach[2] = {};     // full res: reach_0 / reach_1
    ID3D11Texture2D* s = nullptr;       // quarter res: the T state after the last step
    ID3D11Texture2D* fq = nullptr;      // quarter res: f_q of the last content frame
    ID3D11Texture2D* dec = nullptr;     // quarter res: knee_reach's per-texel step
    ID3D11Texture2D* kq = nullptr;      // quarter res: the last round's knee-cap block max
    ID3D11Texture2D* v8 = nullptr;      // eighth res: V8d's gate V8 of the last content frame (before up8 + clamp)
    ID3D11Buffer* flags = nullptr;      // 16 uints (fald_fade_shader.h FADE_FLAG_*)
    unsigned int width = 0, height = 0, wq = 0, hq = 0, w8 = 0, h8 = 0;
    bool hasHistory = false;            // the T state holds a previous frame (CPU-side bookkeeping)
};
FaldFadeDebug FaldFadeDebugViews(const FaldFade* f);
