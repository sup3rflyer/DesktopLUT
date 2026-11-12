// DesktopLUT - shared/dwm_hook_patterns.h
// The byte patterns DwmHook.dll scans dwmcore.dll for, one set per Windows branch. ONE source: DllMain
// (dwm_hook/dllmain.cpp) scans the loaded dwmcore with them, and the host reads the same tables to scan the on-disk
// file before it injects (src/dwmcore_scan.*: the pre-flight, and the offline pattern audit in
// tests/test_dwmcore_scan.cpp). '?' (0x3F) is a wildcard byte, as aob_match_inverse treats it; a pattern byte that
// really is 0x3F cannot be expressed.
//
// Header-only data, internal linkage (namespace-scope const), so DllMain's code is unchanged by the move: the arrays
// and their sizes are exactly the ones it used to define itself. Change a pattern here and run the offline audit
// (DLUT_DWMCORE=<a dwmcore.dll>, DesktopLUT.Tests.exe -tc="dwmcore scan*") before any hardware test.

#pragma once

// ---------------------------------------------------------------------------------------------------------------
// Windows 10 (build < 22000)
const unsigned char COverlayContext_Present_bytes[] = {
	0x48, 0x89, 0x5c, 0x24, 0x08, 0x48, 0x89, 0x74, 0x24, 0x10, 0x57, 0x48, 0x83, 0xec, 0x40, 0x48, 0x8b, 0xb1, 0x20,
	0x2c, 0x00, 0x00, 0x45, 0x8b, 0xd0, 0x48, 0x8b, 0xfa, 0x48, 0x8b, 0xd9, 0x48, 0x85, 0xf6, 0x0f, 0x85
};

const unsigned char COverlayContext_IsCandidateDirectFlipCompatbile_bytes[] = {
	0x48, 0x89, 0x7c, 0x24, 0x20, 0x55, 0x41, 0x54, 0x41, 0x55, 0x41, 0x56, 0x41, 0x57, 0x48, 0x8b, 0xec, 0x48, 0x83,
	0xec, 0x40
};
const unsigned char COverlayContext_OverlaysEnabled_bytes[] = {
	0x75, 0x04, 0x32, 0xc0, 0xc3, 0xcc, 0x83, 0x79, 0x30, 0x01, 0x0f, 0x97, 0xc0, 0xc3
};

// ---------------------------------------------------------------------------------------------------------------
// Windows 11 before 24H2 (build 22000..26099)

/*
 * AOB for function: COverlayContext_Present_bytes_w11
 *
 * 40 53 55 56 57 41 56 41 57 48 81 EC 88 00 00 00 48 8B 05 ?? ?? ?? ?? 48 33 C4 48 89 44 24 78 48
 *
 */
const unsigned char COverlayContext_Present_bytes_w11[] = {
	0x40, 0x53, 0x55, 0x56, 0x57, 0x41, 0x56, 0x41, 0x57, 0x48, 0x81, 0xEC, 0x88, 0x00, 0x00, 0x00, 0x48, 0x8B, 0x05,
	'?', '?', '?', '?', 0x48, 0x33, 0xC4, 0x48, 0x89, 0x44, 0x24, 0x78, 0x48
};

/*
 * AOB for function: COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11
 *
 * 40 55 53 56 57 41 54 41 55 41 56 41 57 48 8B EC 48 83 EC 68 48
 */
const unsigned char COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11[] = {
	0x40, 0x55, 0x53, 0x56, 0x57, 0x41, 0x54, 0x41, 0x55, 0x41, 0x56, 0x41, 0x57, 0x48, 0x8B, 0xEC, 0x48, 0x83, 0xEC,
	0x68, 0x48,
};

/*
 * AOB for function: COverlayContext_OverlaysEnabled_bytes_w11
 *
 * 83 3D ?? ?? ?? ?? ?? 75 04
 */
const unsigned char COverlayContext_OverlaysEnabled_bytes_w11[] = {
	0x83, 0x3D, '?', '?', '?', '?', '?', 0x75, 0x04
};

// ---------------------------------------------------------------------------------------------------------------
// Windows 11 24H2 (build 26100..26199)

/**
 * AOB for function COverlayContext_Present_bytes_w11_24h2
 *
 * 4C 8B DC 56 41 56
 */
const unsigned char COverlayContext_Present_bytes_w11_24h2[] = {
	0x4C, 0x8B, 0xDC, 0x56, 0x41, 0x56
};

const unsigned char COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_24h2[] = {
	0x48, 0x8B, 0xC4, 0x48, 0x89, 0x58, '?', 0x48, 0x89, 0x68, '?', 0x48, 0x89, 0x70, '?', 0x48, 0x89, 0x78, '?', 0x41, 0x56, 0x48, 0x83, 0xEC, 0x20, 0x33, 0xDB
};

// A call to OverlaysEnabled (E8 rel32): the target is the function.
const unsigned char COverlayContext_OverlaysEnabled_bytes_relative_w11_24h2[] = {
	0xE8, '?', '?', '?', '?', 0x84, 0xC0, 0xB8, 0x04, 0x00, 0x00, 0x00
};

// ---------------------------------------------------------------------------------------------------------------
// Windows 11 25H2 (build 26200+)

/**
 * AOB for function COverlayContext_Present_bytes_w11_25h2
 *
 * 40 55 53 56 57 41 54 41 55 41 56 41 57 48 8D 6C 24 F9 48 81 EC F8 00 00 00 48 8B 05 ?? ?? ?? ?? 48 33 C4 48 89 45 EF 4C 8B 65 ?? 48 8B D9
 */
const unsigned char COverlayContext_Present_bytes_w11_25h2[] = {
	0x40, 0x55, 0x53, 0x56, 0x57, 0x41, 0x54, 0x41, 0x55, 0x41, 0x56, 0x41, 0x57, 0x48, 0x8D, 0x6C,
	0x24, 0xF9, 0x48, 0x81, 0xEC, 0xF8, 0x00, 0x00, 0x00, 0x48, 0x8B, 0x05,
	'?', '?', '?', '?', 0x48, 0x33, 0xC4, 0x48, 0x89, 0x45, 0xEF, 0x4C, 0x8B, 0x65, '?', 0x48, 0x8B, 0xD9
};

/**
 * AOB for function COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_25h2
 *
 * 48 8B C4 48 89 58 08 48 89 68 10 48 89 70 18 48 89 78 20 41 56 48 83 EC 20 33 DB
 */
const unsigned char COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_25h2[] = {
	0x48, 0x8B, 0xC4, 0x48, 0x89, 0x58, 0x08, 0x48, 0x89, 0x68, 0x10, 0x48, 0x89, 0x70, 0x18, 0x48,
	0x89, 0x78, 0x20, 0x41, 0x56, 0x48, 0x83, 0xEC, 0x20, 0x33, 0xDB
};

/**
 * AOB for function COverlayContext_OverlaysEnabled_bytes_w11_25h2
 *
 * 83 3D ?? ?? ?? ?? 05 74 09 83 79 28 01 0F 97 C0 C3
 */
const unsigned char COverlayContext_OverlaysEnabled_bytes_w11_25h2[] = {
	0x83, 0x3D, '?', '?', '?', '?', 0x05, 0x74, 0x09, 0x83, 0x79, 0x28, 0x01, 0x0F, 0x97, 0xC0, 0xC3
};

/**
 * AOB for CWindowContext::IsCandidateDirectFlipCompatible in 25H2
 * Forcing this to fail prevents borderless games from bypassing DWM (MPO/DirectFlip)
 */
const unsigned char CWindowContext_IsCandidateDirectFlipCompatible_bytes_w11_25h2[] = {
	0x48, 0x89, 0x5C, 0x24, 0x08, 0x48, 0x89, 0x74, 0x24, 0x10, 0x57, 0x48, 0x83, 0xEC, 0x20, 0x41, 0x8B, 0xD9, 0x48, 0x8B, 0xF2, 0x4C, 0x8B, 0x01, 0x48, 0x8B, 0xF9
};

/**
 * AOB for CCompSwapChain::IsCandidateDirectFlipCompatible in 25H2
 * This is the critical one for modern Fullscreen/Independent Flip
 */
const unsigned char CCompSwapChain_IsCandidateDirectFlipCompatible_bytes_w11_25h2[] = {
	0x48, 0x8B, 0xC4, 0x48, 0x89, 0x58, 0x08, 0x48, 0x89, 0x68, 0x10, 0x48, 0x89, 0x70, 0x18, 0x48, 0x89, 0x78, 0x20, 0x41, 0x56, 0x48, 0x83, 0xEC, 0x20, 0x33, 0xDB, 0x41, 0x8B, 0xF0
};

/**
 * AOB for CCompVisual::IsCandidateForPromotion in 25H2
 * This prevents the window visual from being "promoted" to a hardware overlay (MPO)
 */
const unsigned char CCompVisual_IsCandidateForPromotion_bytes_w11_25h2[] = {
	0x48, 0x89, 0x5C, 0x24, 0x10, 0x48, 0x89, 0x74, 0x24, 0x18, 0x57, 0x48, 0x83, 0xEC, 0x20, 0x48, 0x8B, 0x01, 0x41, 0x8B, 0xD1, 0x48, 0x8B, 0xF1
};

/**
 * AOB for COverlayContext::IsDirectFlipSupportedOnTarget in 25H2
 * Replaces the removed CWindowContext/CCompVisual DirectFlip hooks.
 * Prevents DWM from attempting DirectFlip on any overlay target.
 *
 * 48 89 5C 24 20 55 56 57 48 83 EC 60 48 8B 05 ?? ?? ?? ?? 48 33 C4 48 89 44 24 50 48 8B 81 ?? ?? ?? ?? 49 8B D8 48 8B F1
 */
const unsigned char COverlayContext_IsDirectFlipSupportedOnTarget_bytes_w11_25h2[] = {
	0x48, 0x89, 0x5C, 0x24, 0x20, 0x55, 0x56, 0x57, 0x48, 0x83, 0xEC, 0x60,
	0x48, 0x8B, 0x05, '?', '?', '?', '?', 0x48, 0x33, 0xC4, 0x48, 0x89, 0x44, 0x24, 0x50,
	0x48, 0x8B, 0x81, '?', '?', '?', '?', 0x49, 0x8B, 0xD8, 0x48, 0x8B, 0xF1
};

/**
 * AOB for CGlobalCompositionSurfaceInfo::IsAdvancedDirectFlipCompatible in 25H2
 * Replaces the removed CCompSwapChain DirectFlip/IndependentFlip hooks.
 * Prevents surfaces from being marked as advanced DirectFlip compatible.
 *
 * 40 53 48 83 EC 20 48 8B 89 ?? ?? ?? ?? 48 85 C9 74 ?? 48 83 64 24 30 00
 */
const unsigned char CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_bytes_w11_25h2[] = {
	0x40, 0x53, 0x48, 0x83, 0xEC, 0x20, 0x48, 0x8B, 0x89,
	'?', '?', '?', '?', 0x48, 0x85, 0xC9, 0x74, '?',
	0x48, 0x83, 0x64, 0x24, 0x30, 0x00
};
