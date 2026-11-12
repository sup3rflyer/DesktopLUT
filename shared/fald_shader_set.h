// DesktopLUT - shared/fald_shader_set.h
// The FALD layer's shaders as ONE list: every shader both hosts (src/fald.cpp, dwm_hook/hook_fald.cpp) create, with the
// exact source text, target and entry point they are compiled from (entry "main", flags 0). The hosts create them from
// this list — precompiled (shared/fald_bytecode.h) or, when a blob is absent or refused, compiled from the text — and the
// tests hold the precompiled blobs to D3DCompile of this text, byte for byte (tests/test_fald_bytecode.cpp).
// Header-only (no project-file entry): fald_shader.h's sources + the always-on core's kernels (shared/fald_core2.cpp).
#pragma once
#include <string>

#include "fald_shader.h"
#include "fald_core2.h"

enum FaldShaderId : unsigned int {
    FALD_SH_STAT = 0, FALD_SH_STAT_COMBINE,
    FALD_SH_CONV1, FALD_SH_CONV2, FALD_SH_CONV4, FALD_SH_CONV8,   // pass 2 per sub-offsets-per-thread variant (FALD_CONV_SO)
    FALD_SH_GAIN, FALD_SH_BLUR, FALD_SH_GAIN_BLUR, FALD_SH_DECIDE,
    FALD_SH_PIXEL,                                               // ps_5_0
    FALD_SH_FULLSCREEN_VS,                                       // vs_5_0 (the hook's; the overlay binds its own identical one)
    FALD_SH_STAR_STAT, FALD_SH_STAR_STAT_COMBINE, FALD_SH_STAR_WEIGHT, FALD_SH_STAR_PLAN,
    FALD_SH_PANEL_CLOCK,
    FALD_SH_CORE_Z1, FALD_SH_CORE_Z2, FALD_SH_CORE_C1, FALD_SH_CORE_CX, FALD_SH_CORE_CY,   // shared/fald_core2.cpp, in its order
    FALD_SH_COUNT
};

inline const char* FaldShaderName(unsigned int id) {
    static const char* const names[FALD_SH_COUNT] = {
        "FaldStatCS", "FaldStatCombineCS", "FaldConvCS1", "FaldConvCS2", "FaldConvCS4", "FaldConvCS8",
        "FaldGainCS", "FaldBlurCS", "FaldGainBlurCS", "FaldDecideCS", "FaldPS", "FaldVS",
        "FaldStarStatCS", "FaldStarStatCombineCS", "FaldStarWeightCS", "FaldStarPlanCS", "FaldPanelClockCS",
        "FaldCore2Z1", "FaldCore2Z2", "FaldCore5C1", "FaldCore5CapX", "FaldCore5CapY" };
    return id < FALD_SH_COUNT ? names[id] : "?";
}
inline const char* FaldShaderTarget(unsigned int id) {
    return id == FALD_SH_PIXEL ? "ps_5_0" : (id == FALD_SH_FULLSCREEN_VS ? "vs_5_0" : "cs_5_0");
}
// The whole source text of shader `id`, as the hosts have always assembled it.
inline std::string FaldShaderSource(unsigned int id) {
    const std::string common = g_faldCommonSource;
    const std::string combine = std::string(g_faldZoneCombineDefine) + common;
    switch (id) {
    case FALD_SH_STAT: return common + g_faldStatSource;
    case FALD_SH_STAT_COMBINE: return combine + g_faldStatSource;
    case FALD_SH_CONV1: case FALD_SH_CONV2: case FALD_SH_CONV4: case FALD_SH_CONV8:
        return std::string(g_faldConvSoDefine[id - FALD_SH_CONV1]) + common + g_faldConvSource;
    case FALD_SH_GAIN: return common + g_faldGainSource;
    case FALD_SH_BLUR: return common + g_faldBlurSource;
    case FALD_SH_GAIN_BLUR: return common + g_faldGainBlurSource;
    case FALD_SH_DECIDE: return common + g_faldDecideSource;
    case FALD_SH_PIXEL: return common + g_faldPixelSource;
    case FALD_SH_FULLSCREEN_VS: return std::string(g_faldFullscreenVsSource);
    case FALD_SH_STAR_STAT: return common + g_faldStarStatSource;
    case FALD_SH_STAR_STAT_COMBINE: return combine + g_faldStarStatSource;
    case FALD_SH_STAR_WEIGHT: return common + g_faldStarWeightSource;
    case FALD_SH_STAR_PLAN: return common + g_faldStarPlanSource;
    case FALD_SH_PANEL_CLOCK: return common + g_faldPanelClockSource;
    case FALD_SH_CORE_Z1: case FALD_SH_CORE_Z2: case FALD_SH_CORE_C1: case FALD_SH_CORE_CX: case FALD_SH_CORE_CY:
        return FaldCore2ShaderSource(id - FALD_SH_CORE_Z1, g_faldCommonSource);
    default: return std::string();
    }
}
