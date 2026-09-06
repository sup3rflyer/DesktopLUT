/*
 * Copyright (C) 2021 ledoge
 * Modifications Copyright (C) 2026 Eduu
 *
 * This program is free software: you can redistribute it and/or modify...
 */
#include "pch.h"
#include "dwm_hook_config.h"
#include "hook_log.h"
#include "hook_lut.h"
#include "hook_render.h"

#include <io.h>
#include <string>
#include <sstream>
#include <atomic>
#include <iostream>
#include <iomanip>
#include <cmath>
#include <clocale>
#include <cstring>
#pragma comment (lib, "d3d11.lib")
#pragma comment (lib, "d3dcompiler.lib")
#pragma comment (lib, "dxgi.lib")
#pragma comment (lib, "uuid.lib")
#pragma comment (lib, "dxguid.lib")


#pragma intrinsic(_ReturnAddress)


void* get_relative_address(void* instruction_address, int offset, int instruction_size)
{
	int relative_offset = *(int*)((unsigned char*)instruction_address + offset);

	return (unsigned char*)instruction_address + instruction_size + relative_offset;
}


const unsigned char COverlayContext_Present_bytes[] = {
	0x48, 0x89, 0x5c, 0x24, 0x08, 0x48, 0x89, 0x74, 0x24, 0x10, 0x57, 0x48, 0x83, 0xec, 0x40, 0x48, 0x8b, 0xb1, 0x20,
	0x2c, 0x00, 0x00, 0x45, 0x8b, 0xd0, 0x48, 0x8b, 0xfa, 0x48, 0x8b, 0xd9, 0x48, 0x85, 0xf6, 0x0f, 0x85
};
const int IOverlaySwapChain_IDXGISwapChain_offset = -0x118;

const unsigned char COverlayContext_IsCandidateDirectFlipCompatbile_bytes[] = {
	0x48, 0x89, 0x7c, 0x24, 0x20, 0x55, 0x41, 0x54, 0x41, 0x55, 0x41, 0x56, 0x41, 0x57, 0x48, 0x8b, 0xec, 0x48, 0x83,
	0xec, 0x40
};
const unsigned char COverlayContext_OverlaysEnabled_bytes[] = {
	0x75, 0x04, 0x32, 0xc0, 0xc3, 0xcc, 0x83, 0x79, 0x30, 0x01, 0x0f, 0x97, 0xc0, 0xc3
};

extern const int COverlayContext_DeviceClipBox_offset = -0x120;

const int IOverlaySwapChain_HardwareProtected_offset = -0xbc;

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
const int IOverlaySwapChain_IDXGISwapChain_offset_w11 = 0xE0;

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

int COverlayContext_DeviceClipBox_offset_w11 = 0x466C;

const int IOverlaySwapChain_HardwareProtected_offset_w11 = -0x144;


/**
 * AOB for function COverlayContext_Present_bytes_w11_24h2
 *
 * 4C 8B DC 56 41 56
 */
const unsigned char COverlayContext_Present_bytes_w11_24h2[] = {
	0x4C, 0x8B, 0xDC, 0x56, 0x41, 0x56
};

const int IOverlaySwapChain_IDXGISwapChain_offset_w11_24h2 = 0x108;

const unsigned char COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_24h2[] = {
	0x48, 0x8B, 0xC4, 0x48, 0x89, 0x58, '?', 0x48, 0x89, 0x68, '?', 0x48, 0x89, 0x70, '?', 0x48, 0x89, 0x78, '?', 0x41, 0x56, 0x48, 0x83, 0xEC, 0x20, 0x33, 0xDB
};

const unsigned char COverlayContext_OverlaysEnabled_bytes_relative_w11_24h2[] = {
	0xE8, '?', '?', '?', '?', 0x84, 0xC0, 0xB8, 0x04, 0x00, 0x00, 0x00
};

int COverlayContext_DeviceClipBox_offset_w11_24h2 = 0x53E8;

const int IOverlaySwapChain_HardwareProtected_offset_w11_24h2 = 0x64;


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


int COverlayContext_DeviceClipBox_offset_w11_25h2 = 0x7698;

const int IOverlaySwapChain_HardwareProtected_offset_w11_25h2 = 0x4C;


const int IOverlaySwapChain_GetSwapChain_vtable_offset_w11_25h2 = 0x108;

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


bool isWindows11 = false;
bool isWindows11_24h2 = false;
bool isWindows11_25h2 = false;


static int* g_pOverlayTestMode = NULL;
static int* g_pDisableIndependentFlip = NULL;
static int g_savedOverlayTestMode = 0;
static int g_savedDisableIndependentFlip = 0;
static bool g_overlayTestModePatched = false;
static bool g_disableIndependentFlipPatched = false;
// Hook level controls which hooks are activated. Default 4.
// Overridable via DesktopLUT_HookLevel.flag file content (0-5).
// 0=none (inert diagnostic), 1=Present only, 2=+IsCandidateDirectFlip,
// 3=+fallback DirectFlip hooks, 4=+OverlayTestMode=5 +OverlaysEnabled (inline-patch
// TRUE on 25H2 together with DisableIndependentFlip=1, MinHook on older builds),
// 5=force OverlaysEnabled via MinHook (UNSAFE on 25H2+). Optional letters after the
// digit select the level-4 writes individually — see g_l4* below.
static int g_hookLevel = 4;
// Level-4 sub-selection (diagnostic bisect). Flag file may carry letters after the
// digit: 'o' = OverlayTestMode=5, 'd' = DisableIndependentFlip=1, 'e' = the
// OverlaysEnabled "mov al,1; ret" (force TRUE) inline patch on 25H2.
// "4" alone (or no letters) = production = all three ("4ode").
//
// History (2026-09-06, HANDOFF_HAGS_FLIPQUEUE_2026-09-06.md §9–10): with
// Hardware-Accelerated GPU Scheduling on (WDDM hardware flip queue), mpv composed
// through DWM paces badly (display-resample vsync-jitter 0.13–0.6, hundreds of
// two-vsync glass holds/min). A per-write bisect first blamed the force-TRUE patch,
// because "4od" gave clean numbers — but on 25H2 an un-forced OverlaysEnabled lets
// any full-monitor window BYPASS DWM composition (PresentMon still reports
// "Composed: Flip"; the LUT visibly stops applying on maximize/fullscreen). Windowed
// mpv with this DLL fully inert, and fullscreen mpv with all patches but no LUT
// draw ('n'), pace just as badly: DWM compositing this client under the HAGS flip
// queue is bad by itself and the hook cannot fix it. So the TRUE patch stays
// (LUT coverage). "4od" remains an opt-in bypass for a client that applies the cube
// itself (e.g. mpv --target-lut) and wants Independent-Flip-class pacing.
static bool g_l4OverlayTestMode = true;
static bool g_l4DisableIFlip = true;
static bool g_l4OverlaysEnabledForceTrue = true;
// Diagnostic only ('n' letter): keep every hook/patch installed but make the Present hook
// a pure pass-through (no LUT draw). Separates "DWM composing the client under HAGS" from
// "the LUT pass inside DWM's present".
static bool g_diagNoLutDraw = false;
// HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers\HwSchMode at injection
// (2 = HAGS on, 1 = off, -1 = unreadable). Logged for diagnosis; see
// HANDOFF_HAGS_FLIPQUEUE_2026-09-06.md.
static int g_hwSchMode = -1;
// Saved original bytes for OverlaysEnabled inline-patch (restored on detach)
static unsigned char g_overlaysEnabledOrigBytes[17] = {0};
static unsigned char* g_overlaysEnabledPatchAddr = NULL;
// --- Registry-backed DWM globals: located by value name, never by fixed offset ---
//
// m_dwOverlayTestMode and m_fDisableIndependentFlip are neighbours in
// CCommonRegistryData, but their spacing is NOT stable: dwmcore 10.0.26100.9168
// inserted a 4-byte field between them (0x14 -> 0x18). A fixed-offset guess then
// writes 1 to an unrelated field while still reporting success, which leaves
// independent flip live *and* patches OverlaysEnabled to return TRUE — DWM then
// composites through real overlay planes with OverlayTestMode=5 active and paints
// its debug tint (cyan/magenta/yellow) over the whole desktop.
//
// Registry *value names* are stable across builds where struct offsets are not,
// so both globals are resolved by finding their name string and following the
// store that consumes the registry read:
//     lea  r8, [rip+<L"OverlayTestMode">]   ; 48/4C 8D /r  mod=00 rm=101
//     call [rip+<registry read helper>]
//     mov  dword ptr [rip+<global>], eax    ; 89 /r       mod=00 rm=101

static const unsigned char* FindBytesInImage(const unsigned char* hay, size_t haySize,
                                             const unsigned char* pat, size_t patLen)
{
	if (patLen == 0 || haySize < patLen) return NULL;
	const unsigned char first = pat[0];
	for (size_t i = 0; i + patLen <= haySize; i++) {
		if (hay[i] != first) continue;
		size_t k = 1;
		for (; k < patLen; k++) if (hay[i + k] != pat[k]) break;
		if (k == patLen) return hay + i;
	}
	return NULL;
}

// Window (in bytes) after the name-string lea in which the storing instruction
// must appear. Generous enough to clear the argument setup and the call, tight
// enough that it cannot wander into an unrelated store.
static const size_t kRegistryStoreWindow = 0x80;

static int* DeriveRegistryGlobalInner(const unsigned char* modBase, size_t modSize,
                                      const unsigned char* nameUtf16, size_t nameBytes)
{
	// nameBytes includes the wide NUL, so this only matches whole value names.
	const unsigned char* str = FindBytesInImage(modBase, modSize, nameUtf16, nameBytes);
	if (str == NULL) return NULL;

	for (size_t i = 0; i + 7 <= modSize; i++) {
		const unsigned char* p = modBase + i;
		if ((p[0] != 0x48 && p[0] != 0x4C) || p[1] != 0x8D || (p[2] & 0xC7) != 0x05)
			continue;
		if (p + 7 + *(const int*)(p + 3) != str)
			continue;

		const unsigned char* q   = p + 7;
		const unsigned char* lim = q + kRegistryStoreWindow;
		if (lim > modBase + modSize - 6) lim = modBase + modSize - 6;
		for (; q < lim; q++) {
			if (q[0] != 0x89 || (q[1] & 0xC7) != 0x05) continue;
			const unsigned char* tgt = q + 6 + *(const int*)(q + 2);
			if (tgt >= modBase && tgt + sizeof(int) <= modBase + modSize)
				return (int*)tgt;
			break;
		}
	}
	return NULL;
}

static int* DeriveRegistryGlobal(const unsigned char* modBase, size_t modSize,
                                 const unsigned char* nameUtf16, size_t nameBytes)
{
	int* result = NULL;
	__try { result = DeriveRegistryGlobalInner(modBase, modSize, nameUtf16, nameBytes); }
	__except (EXCEPTION_EXECUTE_HANDLER) { result = NULL; }
	return result;
}

// Publish m_fDisableIndependentFlip to g_pDisableIndependentFlip, or leave it NULL.
// NULL is the safe outcome: OverlaysEnabled is then patched to return FALSE, which
// forces composition instead of trusting a write we could not verify.
static void ResolveDisableIndependentFlip(const unsigned char* modBase, size_t modSize,
                                          int* overlayTestMode)
{
	static const wchar_t kOtmName[] = L"OverlayTestMode";
	static const wchar_t kDifName[] = L"DisableIndependentFlip";

	// Cross-check the technique on this build before trusting it: derive
	// m_dwOverlayTestMode by name and require it to land on the same address the
	// OverlaysEnabled AOB already produced. Two independent methods agreeing is
	// what licenses us to apply the same derivation to the sibling field.
	int* otmByName = DeriveRegistryGlobal(modBase, modSize,
		(const unsigned char*)kOtmName, sizeof(kOtmName));
	if (otmByName == NULL || otmByName != overlayTestMode) {
		log_to_file("DisableIndependentFlip: name-derivation cross-check FAILED — leaving unpatched");
		return;
	}

	int* dif = DeriveRegistryGlobal(modBase, modSize,
		(const unsigned char*)kDifName, sizeof(kDifName));
	if (dif == NULL) {
		log_to_file("DisableIndependentFlip: value name not found — leaving unpatched");
		return;
	}

	// Plausibility: same registry-data struct, and DWORD-aligned.
	INT_PTR delta = (INT_PTR)((const unsigned char*)dif - (const unsigned char*)overlayTestMode);
	INT_PTR absDelta = delta < 0 ? -delta : delta;
	if (absDelta > 0x200 || ((ULONG_PTR)dif & 3) != 0) {
		char msg[160];
		snprintf(msg, sizeof(msg),
			"DisableIndependentFlip: implausible location (delta 0x%llX) — leaving unpatched",
			(unsigned long long)absDelta);
		log_to_file(msg);
		return;
	}

	__try {
		int curVal = *dif;
		if (curVal != 0 && curVal != 1) {
			char msg[160];
			snprintf(msg, sizeof(msg),
				"DisableIndependentFlip: non-boolean value %d at delta 0x%llX — leaving unpatched",
				curVal, (unsigned long long)absDelta);
			log_to_file(msg);
			return;
		}
	}
	__except (EXCEPTION_EXECUTE_HANDLER) {
		log_to_file("DisableIndependentFlip: candidate not readable — leaving unpatched");
		return;
	}

	g_pDisableIndependentFlip = dif;
	{
		char msg[128];
		snprintf(msg, sizeof(msg),
			"DisableIndependentFlip resolved by name (delta 0x%llX from OverlayTestMode)",
			(unsigned long long)absDelta);
		log_to_file(msg);
	}
}

// --- Heartbeat event: signals to host that the DWM hook is active ---
static HANDLE g_heartbeatEvent = NULL;

// --- Host process monitoring: detect orphaned injection ---
// dwm.exe is a Protected Process on Win11 — cannot call OpenProcess() on non-PP processes.
// Instead, poll the process list every 5s using CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS).
static DWORD g_hostPid = 0;
static HANDLE g_hostMonitorThread = NULL;
static HANDLE g_hostMonitorStopEvent = NULL;
static std::atomic<bool> g_hookReverted{false};  // Set when host dies, prevents double-revert in DLL_PROCESS_DETACH

// --- Shared memory IPC: live parameter updates from host ---
static HANDLE g_sharedMemHandle = NULL;
static const DwmHookSharedConfig* g_sharedConfig = NULL;
static uint32_t g_localConfigVersion = 0;

// Local tonemap params per monitor (updated from shared memory)
static LocalTonemapParams g_localTonemap[MAX_DWM_HOOK_MONITORS] = {};
static int g_numLocalTonemap = 0;

// Forward declarations for shared memory functions
static void UpdateLocalTonemapFromShared();

static bool IsProcessAlive(DWORD pid)
{
	HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
	if (snap == INVALID_HANDLE_VALUE) return true;  // Can't check, assume alive

	PROCESSENTRY32W pe{};
	pe.dwSize = sizeof(pe);
	bool found = false;

	if (Process32FirstW(snap, &pe)) {
		do {
			if (pe.th32ProcessID == pid) {
				found = true;
				break;
			}
		} while (Process32NextW(snap, &pe));
	}

	CloseHandle(snap);
	return found;
}

static DWORD WINAPI HostMonitorThreadFunc(LPVOID)
{
	while (true) {
		// Wait 5 seconds or until stop event is signaled
		DWORD result = WaitForSingleObject(g_hostMonitorStopEvent, 5000);
		if (result == WAIT_OBJECT_0) break;  // Stop event signaled — normal shutdown

		// Check if host process is still alive
		if (!IsProcessAlive(g_hostPid)) {
			// Host process terminated — revert DWM to normal
			log_to_file("Host process exited unexpectedly, reverting DWM hook...");

			if (!g_hookReverted.exchange(true)) {
				if (g_overlayTestModePatched && g_pOverlayTestMode != NULL) {
					__try { *g_pOverlayTestMode = g_savedOverlayTestMode; }
					__except (EXCEPTION_EXECUTE_HANDLER) {}
				}
				if (g_disableIndependentFlipPatched && g_pDisableIndependentFlip != NULL) {
					__try { *g_pDisableIndependentFlip = g_savedDisableIndependentFlip; }
					__except (EXCEPTION_EXECUTE_HANDLER) {}
				}
				if (g_overlaysEnabledPatchAddr != NULL) {
					DWORD oldProt;
					if (VirtualProtect(g_overlaysEnabledPatchAddr, 17, PAGE_EXECUTE_READWRITE, &oldProt)) {
						memcpy(g_overlaysEnabledPatchAddr, g_overlaysEnabledOrigBytes, 17);
						VirtualProtect(g_overlaysEnabledPatchAddr, 17, oldProt, &oldProt);
						FlushInstructionCache(GetCurrentProcess(), g_overlaysEnabledPatchAddr, 17);
					}
					g_overlaysEnabledPatchAddr = NULL;
				}
				MH_DisableHook(MH_ALL_HOOKS);
			}

			// Close heartbeat event so host watchdog (if restarted) sees it as gone
			if (g_heartbeatEvent) {
				CloseHandle(g_heartbeatEvent);
				g_heartbeatEvent = NULL;
			}

			log_to_file("DWM hook reverted (host exit) — DLL remains loaded but inert");
			break;
		}
	}
	return 0;
}

// Read host PID from staging file written by the injector
static DWORD ReadHostPid()
{
	char pidPath[MAX_PATH] = {0};
	ExpandEnvironmentStringsA("%SYSTEMROOT%\\Temp\\DesktopLUT_luts\\host.pid", pidPath, sizeof(pidPath));

	FILE* pf = fopen(pidPath, "r");
	if (!pf) return 0;

	DWORD pid = 0;
	if (fscanf(pf, "%lu", &pid) != 1) pid = 0;
	fclose(pf);
	return pid;
}

bool aob_match_inverse(const void* buf1, const void* mask, const int buf_len)
{
	for (int i = 0; i < buf_len; ++i)
	{
		if (((unsigned char*)buf1)[i] != ((unsigned char*)mask)[i] && ((unsigned char*)mask)[i] != '?')
		{
			return true;
		}
	}
	return false;
}


// Suspicious-change debounce: require N consecutive identical observations of a
// monitor-set shrink or HDR-flag flip before accepting, to ride out transient DXGI
// hiccups (e.g., fullscreen video temporarily hiding a monitor, or HDR mid-transition
// misreporting a monitor's color space). Benign changes (topology growth, tonemap
// param tweaks) apply immediately.
static int g_suspiciousChangeCount = 0;
#define SUSPICIOUS_CHANGE_DEBOUNCE 3

static void UpdateLocalTonemapFromShared() {
	if (!g_sharedConfig) return;

	uint32_t v1 = g_sharedConfig->version;
	if (v1 == g_localConfigVersion) return;
	if (v1 & 1) return;  // Odd version = writer in progress

	std::atomic_thread_fence(std::memory_order_acquire);

	DwmHookSharedConfig local;
	memcpy(&local, (const void*)g_sharedConfig, sizeof(local));

	std::atomic_thread_fence(std::memory_order_acquire);
	uint32_t v2 = g_sharedConfig->version;
	if (v1 != v2) return;

	g_localConfigVersion = local.version;

	// Clamp numMonitors to prevent OOB from shared memory
	uint32_t numMons = (local.numMonitors < MAX_DWM_HOOK_MONITORS) ? local.numMonitors : MAX_DWM_HOOK_MONITORS;

	// Identity beacon session state — applied immediately, never debounced (a session is a
	// short host-driven burst; the colour table is only meaningful while it is active).
	if (local.beaconActive && !g_beaconActive) {
		char bmsg[96];
		snprintf(bmsg, sizeof(bmsg), "beacon: session %u active (size %u)", local.beaconGeneration, local.beaconSize);
		log_to_file(bmsg);
	}
	g_beaconActive = local.beaconActive;
	g_beaconGeneration = local.beaconGeneration;
	g_beaconSize = local.beaconSize;
	g_numBeaconColors = 0;
	for (uint32_t i = 0; i < numMons && g_numBeaconColors < 16; i++) {
		g_beaconColors[g_numBeaconColors].left = local.monitors[i].left;
		g_beaconColors[g_numBeaconColors].top = local.monitors[i].top;
		g_beaconColors[g_numBeaconColors].colorId = local.monitors[i].beaconColorId;
		g_numBeaconColors++;
	}

	// Classify this update as suspicious if:
	//  (a) monitor count shrank, or
	//  (b) a monitor at a position present in both old and new state flipped HDR flag.
	// Suspicious updates are held for SUSPICIOUS_CHANGE_DEBOUNCE consecutive updates
	// before the new monitor state is applied. Tonemap params still update immediately.
	bool suspicious = false;
	if (g_numMonitorHdrStates > 0) {
		if ((int)numMons < g_numMonitorHdrStates) {
			suspicious = true;
		} else {
			for (uint32_t i = 0; i < numMons; i++) {
				for (int j = 0; j < g_numMonitorHdrStates; j++) {
					if ((int)local.monitors[i].left == g_monitorHdrStates[j].left &&
					    (int)local.monitors[i].top == g_monitorHdrStates[j].top) {
						bool newHdr = (local.monitors[i].isHdr != 0);
						if (newHdr != g_monitorHdrStates[j].isHdr) { suspicious = true; }
						break;
					}
				}
				if (suspicious) break;
			}
		}
	}

	bool applyMonitorState = true;
	if (suspicious) {
		g_suspiciousChangeCount++;
		if (g_suspiciousChangeCount < SUSPICIOUS_CHANGE_DEBOUNCE) {
			applyMonitorState = false;
		}
		if (g_suspiciousChangeCount == 1) {
			char msg[160];
			snprintf(msg, sizeof(msg),
				"Monitor state change looks suspicious (count %d->%u) — debouncing",
				g_numMonitorHdrStates, numMons);
			log_to_file(msg);
		} else if (g_suspiciousChangeCount == SUSPICIOUS_CHANGE_DEBOUNCE) {
			log_to_file("Monitor state change persisted — accepting after debounce");
		}
	} else {
		if (g_suspiciousChangeCount > 0) {
			log_to_file("Monitor state change canceled (state recovered)");
		}
		g_suspiciousChangeCount = 0;
	}

	// Update monitor HDR states from shared memory (gated on debounce result).
	// Also invalidate context position cache if topology actually changed, so stale
	// ctx→monitor mappings don't route rendering to the wrong pipeline.
	if (applyMonitorState) {
		bool topologyChanged = ((int)numMons != g_numMonitorHdrStates);
		if (!topologyChanged) {
			for (uint32_t i = 0; i < numMons; i++) {
				if ((int)local.monitors[i].left != g_monitorHdrStates[i].left ||
				    (int)local.monitors[i].top != g_monitorHdrStates[i].top) {
					topologyChanged = true; break;
				}
			}
		}

		g_numMonitorHdrStates = 0;
		for (uint32_t i = 0; i < numMons && g_numMonitorHdrStates < 16; i++) {
			auto& mc = local.monitors[i];
			auto& ms = g_monitorHdrStates[g_numMonitorHdrStates];
			ms.left = mc.left;
			ms.top = mc.top;
			ms.width = mc.width;
			ms.height = mc.height;
			ms.bpc = mc.bpc;
			ms.isHdr = (mc.isHdr != 0);
			g_numMonitorHdrStates++;
		}

		if (topologyChanged) {
			// Every context re-resolves on its next present (the cache is the only "seen" list).
			ResetContextRouting();
			// Pins were validated against the attach-time topology; a different one makes them
			// meaningless (and the routing file's mon lines will no longer match).
			g_routingPinsValid = false;
			g_numRoutingPins = 0;
			g_routingConfirmed = false;
		}
	}

	// Update local tonemap params — tied to monitor state so we also gate on debounce,
	// otherwise a shrunken-but-held monitor would lose its tonemap entry mid-Present.
	if (applyMonitorState) {
		g_numLocalTonemap = 0;
		for (uint32_t i = 0; i < numMons && g_numLocalTonemap < MAX_DWM_HOOK_MONITORS; i++) {
			auto& mc = local.monitors[i];
			auto& tp = g_localTonemap[g_numLocalTonemap];
			bool prevDynamic = tp.dynamicPeak;
			tp.left = mc.left;
			tp.top = mc.top;
			tp.enabled = (mc.tonemapEnabled != 0);
			tp.curve = mc.tonemapCurve;
			tp.sourcePeakNits = mc.sourcePeakNits;
			tp.targetPeakNits = mc.targetPeakNits;
			tp.dynamicPeak = (mc.dynamicPeak != 0);
			tp.pqSourcePeak = LinearToPQ(mc.sourcePeakNits / 10000.0f);
			tp.pqTargetPeak = LinearToPQ(mc.targetPeakNits / 10000.0f);
			if (tp.dynamicPeak != prevDynamic) {
				char msg[128];
				snprintf(msg, sizeof(msg), "Tonemap update: mon(%d,%d) dynamic=%d src=%.0f tgt=%.0f pqSrc=%.4f pqTgt=%.4f",
					tp.left, tp.top, tp.dynamicPeak ? 1 : 0,
					tp.sourcePeakNits, tp.targetPeakNits, tp.pqSourcePeak, tp.pqTargetPeak);
				log_to_file(msg);
			}
			g_numLocalTonemap++;
		}
	}

	if (local.lutReloadFlag) {
		log_to_file("Shared memory: lutReloadFlag set (not yet implemented)");
	}
}

LocalTonemapParams* FindTonemapForMonitor(int left, int top) {
	for (int i = 0; i < g_numLocalTonemap; i++) {
		if (g_localTonemap[i].left == left && g_localTonemap[i].top == top)
			return &g_localTonemap[i];
	}
	return NULL;
}


typedef struct rectVec
{
	struct tagRECT* start;
	struct tagRECT* end;
	struct tagRECT* cap;
} rectVec;

typedef long (COverlayContext_Present_t)(void*, void*, unsigned int, rectVec*, unsigned int, bool);
typedef long long (COverlayContext_Present_24h2_t)(void*, void*, unsigned int, rectVec*, int, void*, bool);



COverlayContext_Present_t* COverlayContext_Present_orig = NULL;
COverlayContext_Present_t* COverlayContext_Present_real_orig = NULL;

COverlayContext_Present_24h2_t* COverlayContext_Present_orig_24h2 = NULL;
COverlayContext_Present_24h2_t* COverlayContext_Present_real_orig_24h2 = NULL;

// SEH-protected overlay swapchain info extraction.
// Reads hwProtected flag and IDXGISwapChain pointer from DWM internal structs via hardcoded offsets.
// Returns false on SEH exception (offset mismatch after Windows update) — caller should skip LUT.
static bool ReadOverlaySwapChainInfo(void* overlaySwapChain, bool& hwProtected, IDXGISwapChain*& swapChain)
{
	__try
	{
		hwProtected = false;
		swapChain = NULL;

		if (isWindows11_24h2)
			hwProtected = *((bool*)overlaySwapChain + IOverlaySwapChain_HardwareProtected_offset_w11_24h2);
		else if (isWindows11)
			hwProtected = *((bool*)overlaySwapChain + IOverlaySwapChain_HardwareProtected_offset_w11);
		else
			hwProtected = *((bool*)overlaySwapChain + IOverlaySwapChain_HardwareProtected_offset);

		if (!hwProtected) {
			if (isWindows11_24h2) {
				swapChain = *(IDXGISwapChain**)((unsigned char*)overlaySwapChain +
					IOverlaySwapChain_IDXGISwapChain_offset_w11_24h2);
			} else if (isWindows11) {
				int sub = *(int*)((unsigned char*)overlaySwapChain - 4);
				void* real = (unsigned char*)overlaySwapChain - sub - 0x1b0;
				swapChain = *(IDXGISwapChain**)((unsigned char*)real +
					IOverlaySwapChain_IDXGISwapChain_offset_w11);
			} else {
				swapChain = *(IDXGISwapChain**)((unsigned char*)overlaySwapChain +
					IOverlaySwapChain_IDXGISwapChain_offset);
			}
		}
		return true;
	}
	__except (EXCEPTION_EXECUTE_HANDLER)
	{
		LOG_ONLY_ONCE("SEH exception reading overlay swapchain — skipping LUT application");
		hwProtected = false;
		swapChain = NULL;
		return false;
	}
}

long long COverlayContext_Present_hook_24h2(void* self, void* overlaySwapChain, unsigned int a3, rectVec* rectVec,
	int a5, void* a6, bool a7)
{
	if (g_diagNoLutDraw)
		return COverlayContext_Present_orig_24h2(self, overlaySwapChain, a3, rectVec, a5, a6, a7);

	// Check for shared memory updates (live tonemap param changes from host)
	UpdateLocalTonemapFromShared();

	{
		LOG_ONLY_ONCE("I am inside COverlayContext::Present hook inside the main if condition")
		std::stringstream overlay_swapchain_message;
		overlay_swapchain_message << "OverlaySwapChain address: 0x" << std::hex << overlaySwapChain
			<< " -- windows 11 25h2: " << isWindows11_25h2
			<< " -- windows 11 24h2: " << isWindows11_24h2
			<< " -- " << "windows 11: " << isWindows11;
		LOG_ONLY_ONCE(overlay_swapchain_message.str().c_str())

		if (!rectVec || !rectVec->start || rectVec->end < rectVec->start) {
			return COverlayContext_Present_orig_24h2(self, overlaySwapChain, a3, rectVec, a5, a6, a7);
		}

		if (isWindows11_25h2)
		{
			bool success = false;

			ID3D11Texture2D* backBuffer = GetBackBuffer_25H2(overlaySwapChain);
			if (backBuffer)
			{
				if (ApplyLUTDirect(self, backBuffer, rectVec->start, rectVec->end - rectVec->start))
				{
					SetLUTActive(self);
					success = true;
				}
				backBuffer->Release();
			}

			// No fallback probing — ApplyLUTDirect returning false means
			// this monitor has no LUT configured, not a failure to try harder.

			if (!success)
			{
				UnsetLUTActive(self);
			}
		}
		else
		{
			bool hwProtected = false;
			IDXGISwapChain* swapChain = NULL;

			if (!ReadOverlaySwapChainInfo(overlaySwapChain, hwProtected, swapChain))
			{
				UnsetLUTActive(self);
			}
			else if (hwProtected)
			{
				LOG_ONLY_ONCE("Hardware protected - unsetting LUT active")
				UnsetLUTActive(self);
			}
			else if (swapChain != NULL && ApplyLUT(self, swapChain, rectVec->start, rectVec->end - rectVec->start))
			{
				LOG_ONLY_ONCE("Setting LUTactive")
				SetLUTActive(self);
			}
			else
			{
				LOG_ONLY_ONCE("Un-setting LUTactive")
				UnsetLUTActive(self);
			}
		}
	}

	return COverlayContext_Present_orig_24h2(self, overlaySwapChain, a3, rectVec, a5, a6, a7);
}


long COverlayContext_Present_hook(void* self, void* overlaySwapChain, unsigned int a3, rectVec* rectVec,
                                  unsigned int a5, bool a6)
{
	UpdateLocalTonemapFromShared();

	if (_ReturnAddress() < (void*)COverlayContext_Present_real_orig)
	{
		LOG_ONLY_ONCE("I am inside COverlayContext::Present hook inside the main if condition")

		if (!rectVec || !rectVec->start || rectVec->end < rectVec->start) {
			return COverlayContext_Present_orig(self, overlaySwapChain, a3, rectVec, a5, a6);
		}

		bool hwProtected = false;
		IDXGISwapChain* swapChain = NULL;

		if (!ReadOverlaySwapChainInfo(overlaySwapChain, hwProtected, swapChain))
		{
			UnsetLUTActive(self);
		}
		else if (hwProtected)
		{
			LOG_ONLY_ONCE("Hardware protected - unsetting LUT active")
			UnsetLUTActive(self);
		}
		else if (swapChain != NULL && ApplyLUT(self, swapChain, rectVec->start, rectVec->end - rectVec->start))
		{
			LOG_ONLY_ONCE("Setting LUTactive")
			SetLUTActive(self);
		}
		else
		{
			LOG_ONLY_ONCE("Un-setting LUTactive")
			UnsetLUTActive(self);
		}
	}

	return COverlayContext_Present_orig(self, overlaySwapChain, a3, rectVec, a5, a6);
}

typedef bool (CWindowContext_IsCandidateDirectFlipCompatbile_t)(void*, void*, bool);
CWindowContext_IsCandidateDirectFlipCompatbile_t* CWindowContext_IsCandidateDirectFlipCompatbile_orig = NULL;

// Check if any active processing (LUT or tonemap) requires DWM composition
static bool HasActiveHookProcessing() {
	if (numLuts > 0) return true;
	for (int i = 0; i < g_numLocalTonemap; i++) {
		if (g_localTonemap[i].enabled) return true;
	}
	return false;
}

bool CWindowContext_IsCandidateDirectFlipCompatbile_hook(void* self, void* a2, bool a3)
{
	if (HasActiveHookProcessing())
	{
		return false;
	}
	return CWindowContext_IsCandidateDirectFlipCompatbile_orig(self, a2, a3);
}

typedef bool (CCompSwapChain_IsCandidateDirectFlipCompatbile_t)(void*, void*, bool);
CCompSwapChain_IsCandidateDirectFlipCompatbile_t* CCompSwapChain_IsCandidateDirectFlipCompatbile_orig = NULL;

bool CCompSwapChain_IsCandidateDirectFlipCompatbile_hook(void* self, void* a2, bool a3)
{
	if (HasActiveHookProcessing())
	{
		return false;
	}
	return CCompSwapChain_IsCandidateDirectFlipCompatbile_orig(self, a2, a3);
}

typedef bool (CCompVisual_IsCandidateForPromotion_t)(void*, void*, void*);
CCompVisual_IsCandidateForPromotion_t* CCompVisual_IsCandidateForPromotion_orig = NULL;

bool CCompVisual_IsCandidateForPromotion_hook(void* self, void* a2, void* a3)
{
	if (HasActiveHookProcessing())
	{
		return false;
	}
	return CCompVisual_IsCandidateForPromotion_orig(self, a2, a3);
}

typedef bool (CCompSwapChain_IsCandidateIndependentFlipCompatible_t)(void*);
CCompSwapChain_IsCandidateIndependentFlipCompatible_t* CCompSwapChain_IsCandidateIndependentFlipCompatible_orig = NULL;

bool CCompSwapChain_IsCandidateIndependentFlipCompatible_hook(void* self)
{
	if (HasActiveHookProcessing())
	{
		return false;
	}
	return CCompSwapChain_IsCandidateIndependentFlipCompatible_orig(self);
}

// 25H2 replacement hooks for removed CWindowContext/CCompVisual/CCompSwapChain DirectFlip functions
typedef bool (COverlayContext_IsDirectFlipSupportedOnTarget_t)(void*, void*, void*);
COverlayContext_IsDirectFlipSupportedOnTarget_t* COverlayContext_IsDirectFlipSupportedOnTarget_orig = NULL;

bool COverlayContext_IsDirectFlipSupportedOnTarget_hook(void* self, void* a2, void* a3)
{
	if (IsLUTActive(self))
	{
		return false;
	}
	return COverlayContext_IsDirectFlipSupportedOnTarget_orig(self, a2, a3);
}

typedef bool (CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_t)(void*);
CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_t* CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_orig = NULL;

bool CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_hook(void* self)
{
	if (HasActiveHookProcessing())
	{
		return false;
	}
	return CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_orig(self);
}

typedef bool (COverlayContext_IsCandidateDirectFlipCompatbile_t)(void*, void*, void*, void*, int, unsigned int, bool,
                                                                 bool);
typedef bool (COverlayContext_IsCandidateDirectFlipCompatbile_24h2_t)(void*, void*, void*, void*, unsigned int, bool);

COverlayContext_IsCandidateDirectFlipCompatbile_t* COverlayContext_IsCandidateDirectFlipCompatbile_orig;
COverlayContext_IsCandidateDirectFlipCompatbile_24h2_t* COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2;

bool COverlayContext_IsCandidateDirectFlipCompatbile_hook_24h2(void* self, void* a2, void* a3, void* a4, unsigned int a5,
	bool a6)
{
	if (IsLUTActive(self))
	{
		return false;
	}
	return COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2(self, a2, a3, a4, a5, a6);
}

bool COverlayContext_IsCandidateDirectFlipCompatbile_hook(void* self, void* a2, void* a3, void* a4, int a5,
                                                          unsigned int a6, bool a7, bool a8)
{
	if (IsLUTActive(self))
	{
		return false;
	}
	return COverlayContext_IsCandidateDirectFlipCompatbile_orig(self, a2, a3, a4, a5, a6, a7, a8);
}

typedef bool (COverlayContext_OverlaysEnabled_t)(void*);

COverlayContext_OverlaysEnabled_t* COverlayContext_OverlaysEnabled_orig  = NULL;

bool COverlayContext_OverlaysEnabled_hook(void* self)
{
	if (IsLUTActive(self))
	{
		return false;
	}
	return COverlayContext_OverlaysEnabled_orig(self);
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD fdwReason, LPVOID lpReserved)
{
	switch (fdwReason)
	{
	case DLL_PROCESS_ATTACH:
		{
			// Force C locale so sscanf("%f") always uses '.' as decimal separator,
			// regardless of the system locale. Without this, .cube LUT parsing breaks
			// on European locales where ',' is the decimal separator.
			setlocale(LC_NUMERIC, "C");

			log_to_file("DLL_PROCESS_ATTACH: DllMain entered");
			HMODULE dwmcore = GetModuleHandle(L"dwmcore.dll");
			if (!dwmcore) {
				log_to_file("ERROR: dwmcore.dll not found — cannot set up hooks");
				return FALSE;
			}
			MODULEINFO moduleInfo;
			if (!GetModuleInformation(GetCurrentProcess(), dwmcore, &moduleInfo, sizeof moduleInfo)) {
				log_to_file("ERROR: GetModuleInformation failed for dwmcore.dll");
				return FALSE;
			}

			OSVERSIONINFOEX versionInfo;
			ZeroMemory(&versionInfo, sizeof OSVERSIONINFOEX);
			versionInfo.dwOSVersionInfoSize = sizeof OSVERSIONINFOEX;
			versionInfo.dwBuildNumber = 22000;


			OSVERSIONINFOEX versionInfo24h2;
			ZeroMemory(&versionInfo24h2, sizeof OSVERSIONINFOEX);
			versionInfo24h2.dwOSVersionInfoSize = sizeof OSVERSIONINFOEX;
			versionInfo24h2.dwBuildNumber = 26100;


			OSVERSIONINFOEX versionInfo25h2;
			ZeroMemory(&versionInfo25h2, sizeof OSVERSIONINFOEX);
			versionInfo25h2.dwOSVersionInfoSize = sizeof OSVERSIONINFOEX;
			versionInfo25h2.dwBuildNumber = 26200;


			ULONGLONG dwlConditionMask = 0;
			VER_SET_CONDITION(dwlConditionMask, VER_BUILDNUMBER, VER_GREATER_EQUAL);

			if (VerifyVersionInfo(&versionInfo25h2, VER_BUILDNUMBER, dwlConditionMask))
			{
				isWindows11_25h2 = true;
			}
			else if (VerifyVersionInfo(&versionInfo24h2, VER_BUILDNUMBER, dwlConditionMask))
			{
				isWindows11_24h2 = true;
			}
			else if (VerifyVersionInfo(&versionInfo, VER_BUILDNUMBER, dwlConditionMask))
			{
				isWindows11 = true;
			}
			else
			{
				isWindows11 = false;
			}

			// Read monitor metadata written by the injector (DXGI can't run inside DWM)
			{
				char monitorsPath[MAX_PATH];
				ExpandEnvironmentStringsA(LUT_FOLDER "\\monitors.dat", monitorsPath, sizeof(monitorsPath));
				FILE* mf = fopen(monitorsPath, "r");
				if (mf) {
					int count = 0;
					if (fscanf(mf, "%d", &count) == 1) {
						for (int i = 0; i < count && g_numMonitorHdrStates < 16; i++) {
							int left, top, w, h, bpc, hdr;
							if (fscanf(mf, "%d %d %d %d %d %d", &left, &top, &w, &h, &bpc, &hdr) == 6) {
								auto& ms = g_monitorHdrStates[g_numMonitorHdrStates];
								ms.left = left; ms.top = top;
								ms.width = (UINT)w; ms.height = (UINT)h;
								ms.bpc = (UINT)bpc; ms.isHdr = (hdr != 0);

								char msg[256];
								snprintf(msg, sizeof(msg), "monitors.dat: (%d,%d) %s bpc=%u %ux%u",
									left, top, ms.isHdr ? "HDR" : "SDR", ms.bpc, ms.width, ms.height);
								log_to_file(msg);

								g_numMonitorHdrStates++;
							}
						}
					}
					fclose(mf);
					char msg[64];
					snprintf(msg, sizeof(msg), "Loaded %d monitors from monitors.dat", g_numMonitorHdrStates);
					log_to_file(msg);
				} else {
					log_to_file("WARNING: monitors.dat not found, position matching disabled");
				}
				g_hdrStatesDetected = true;
				// Twin-panel routing pins persisted by this dwm.exe's previous injection (25H2).
				LoadRoutingPins();
			}

			// Open shared memory for live IPC from host
			{
				g_sharedMemHandle = OpenFileMappingW(FILE_MAP_READ, FALSE, DWM_HOOK_CONFIG_NAME);
				if (g_sharedMemHandle) {
					g_sharedConfig = (const DwmHookSharedConfig*)MapViewOfFile(
						g_sharedMemHandle, FILE_MAP_READ, 0, 0, sizeof(DwmHookSharedConfig));
					if (g_sharedConfig) {
						log_to_file("Shared memory opened OK");
						// Initial read of tonemap params
						UpdateLocalTonemapFromShared();
					} else {
						log_to_file("WARNING: MapViewOfFile failed for shared memory");
						CloseHandle(g_sharedMemHandle);
						g_sharedMemHandle = NULL;
					}
				} else {
					log_to_file("WARNING: Shared memory not available — live tonemap IPC disabled");
				}
			}

			if (isWindows11_25h2)
			{
				for (size_t i = 0; i <= moduleInfo.SizeOfImage - sizeof COverlayContext_OverlaysEnabled_bytes_w11_25h2; i++)
				{
					unsigned char* address = (unsigned char*)dwmcore + i;
					if (!COverlayContext_Present_orig_24h2 && sizeof COverlayContext_Present_bytes_w11_25h2 <= moduleInfo.
						SizeOfImage - i && !aob_match_inverse(address, COverlayContext_Present_bytes_w11_25h2,
							sizeof COverlayContext_Present_bytes_w11_25h2))
					{
						COverlayContext_Present_orig_24h2 = (COverlayContext_Present_24h2_t*)address;
						COverlayContext_Present_real_orig_24h2 = COverlayContext_Present_orig_24h2;
					}
					else if (!COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2 && sizeof
						COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_25h2 <= moduleInfo.SizeOfImage - i && !
						aob_match_inverse(
							address, COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_25h2,
							sizeof COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_25h2))
					{
						COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2 = (
							COverlayContext_IsCandidateDirectFlipCompatbile_24h2_t*)address;
					}
					else if (!CWindowContext_IsCandidateDirectFlipCompatbile_orig && sizeof CWindowContext_IsCandidateDirectFlipCompatible_bytes_w11_25h2
						<= moduleInfo.SizeOfImage - i && !aob_match_inverse(
							address, CWindowContext_IsCandidateDirectFlipCompatible_bytes_w11_25h2,
							sizeof CWindowContext_IsCandidateDirectFlipCompatible_bytes_w11_25h2))
					{
						CWindowContext_IsCandidateDirectFlipCompatbile_orig = (CWindowContext_IsCandidateDirectFlipCompatbile_t*)address;
					}
					else if (!CCompSwapChain_IsCandidateDirectFlipCompatbile_orig && sizeof CCompSwapChain_IsCandidateDirectFlipCompatible_bytes_w11_25h2
						<= moduleInfo.SizeOfImage - i && !aob_match_inverse(
							address, CCompSwapChain_IsCandidateDirectFlipCompatible_bytes_w11_25h2,
							sizeof CCompSwapChain_IsCandidateDirectFlipCompatible_bytes_w11_25h2))
					{
						CCompSwapChain_IsCandidateDirectFlipCompatbile_orig = (CCompSwapChain_IsCandidateDirectFlipCompatbile_t*)address;
					}
					else if (!CCompVisual_IsCandidateForPromotion_orig && sizeof CCompVisual_IsCandidateForPromotion_bytes_w11_25h2
						<= moduleInfo.SizeOfImage - i && !aob_match_inverse(
							address, CCompVisual_IsCandidateForPromotion_bytes_w11_25h2,
							sizeof CCompVisual_IsCandidateForPromotion_bytes_w11_25h2))
					{
						CCompVisual_IsCandidateForPromotion_orig = (CCompVisual_IsCandidateForPromotion_t*)address;
					}
					else if (!COverlayContext_IsDirectFlipSupportedOnTarget_orig && sizeof COverlayContext_IsDirectFlipSupportedOnTarget_bytes_w11_25h2
						<= moduleInfo.SizeOfImage - i && !aob_match_inverse(
							address, COverlayContext_IsDirectFlipSupportedOnTarget_bytes_w11_25h2,
							sizeof COverlayContext_IsDirectFlipSupportedOnTarget_bytes_w11_25h2))
					{
						COverlayContext_IsDirectFlipSupportedOnTarget_orig = (COverlayContext_IsDirectFlipSupportedOnTarget_t*)address;
					}
					else if (!CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_orig && sizeof CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_bytes_w11_25h2
						<= moduleInfo.SizeOfImage - i && !aob_match_inverse(
							address, CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_bytes_w11_25h2,
							sizeof CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_bytes_w11_25h2))
					{
						CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_orig = (CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_t*)address;
					}
					else if (!COverlayContext_OverlaysEnabled_orig && sizeof COverlayContext_OverlaysEnabled_bytes_w11_25h2
						<= moduleInfo.SizeOfImage - i && !aob_match_inverse(
							address, COverlayContext_OverlaysEnabled_bytes_w11_25h2,
							sizeof COverlayContext_OverlaysEnabled_bytes_w11_25h2))
					{
						COverlayContext_OverlaysEnabled_orig = (COverlayContext_OverlaysEnabled_t*)address;




						int rip_offset = *(int*)(address + 2);
						int* candidatePtr = (int*)(address + 7 + rip_offset);
						// Validate computed address is within dwmcore.dll image range
						if ((unsigned char*)candidatePtr >= (unsigned char*)dwmcore &&
							(unsigned char*)candidatePtr < (unsigned char*)dwmcore + moduleInfo.SizeOfImage)
						{
							g_pOverlayTestMode = candidatePtr;

							ResolveDisableIndependentFlip((const unsigned char*)dwmcore,
								moduleInfo.SizeOfImage, candidatePtr);
						}
						else
						{
							g_pOverlayTestMode = NULL;
							LOG_ONLY_ONCE("WARNING: g_pOverlayTestMode address outside dwmcore.dll range — skipped");
						}



						// CCompSwapChain_IsCandidateIndependentFlipCompatible: not scanned on
						// 25H2 — DisableIndependentFlip=1 handles independent flip suppression.
					}
					if (COverlayContext_Present_orig_24h2 && COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2 &&
						COverlayContext_OverlaysEnabled_orig)
					{
						break;
					}
				}
			{
					// PDB-derived fallback: if AOB scan missed OverlaysEnabled,
					// OverlayTestMode, or DisableIndependentFlip, resolve them from
					// known RVAs (dwmcore.dll 10.0.26100.8115, KB5089549).
					// Fallback: if the main else-if chain missed OverlaysEnabled
					// (can happen when chain order prevents match), do a dedicated pass.
					if (!COverlayContext_OverlaysEnabled_orig && moduleInfo.SizeOfImage >= 17) {
						for (size_t j = 0; j < moduleInfo.SizeOfImage - 17; j++) {
							unsigned char* c = (unsigned char*)dwmcore + j;
							if (c[0] == 0x83 && c[1] == 0x3D &&
							    c[6] == 0x05 && c[7] == 0x74 && c[8] == 0x09 &&
							    c[9] == 0x83 && c[10] == 0x79 && c[11] == 0x28 &&
							    c[12] == 0x01 && c[13] == 0x0F && c[14] == 0x97 &&
							    c[15] == 0xC0 && c[16] == 0xC3)
							{
								COverlayContext_OverlaysEnabled_orig = (COverlayContext_OverlaysEnabled_t*)c;
								log_to_file("OverlaysEnabled found via dedicated scan");

								int rip_offset = *(int*)(c + 2);
								int* otmCandidate = (int*)(c + 7 + rip_offset);
								if ((unsigned char*)otmCandidate >= (unsigned char*)dwmcore &&
								    (unsigned char*)otmCandidate < (unsigned char*)dwmcore + moduleInfo.SizeOfImage)
								{
									g_pOverlayTestMode = otmCandidate;
									ResolveDisableIndependentFlip((const unsigned char*)dwmcore,
										moduleInfo.SizeOfImage, otmCandidate);
								}
								break;
							}
						}
						if (!COverlayContext_OverlaysEnabled_orig)
							log_to_file("WARNING: OverlaysEnabled not found");
					}
				}
			}
			else if (isWindows11_24h2)
			{
				for (size_t i = 0; i <= moduleInfo.SizeOfImage - sizeof COverlayContext_OverlaysEnabled_bytes_relative_w11_24h2; i++)
				{
					unsigned char* address = (unsigned char*)dwmcore + i;
					if (!COverlayContext_Present_orig_24h2 && sizeof COverlayContext_Present_bytes_w11_24h2 <= moduleInfo.
						SizeOfImage - i && !aob_match_inverse(address, COverlayContext_Present_bytes_w11_24h2,
							sizeof COverlayContext_Present_bytes_w11_24h2))
					{
						COverlayContext_Present_orig_24h2 = (COverlayContext_Present_24h2_t*)address;
						COverlayContext_Present_real_orig_24h2 = COverlayContext_Present_orig_24h2;
					}
					else if (!COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2 && sizeof
						COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_24h2 <= moduleInfo.SizeOfImage - i && !
						aob_match_inverse(
							address, COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_24h2,
							sizeof COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_24h2))
					{
						COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2 = (
							COverlayContext_IsCandidateDirectFlipCompatbile_24h2_t*)address;
					}
					else if (!COverlayContext_OverlaysEnabled_orig && sizeof COverlayContext_OverlaysEnabled_bytes_relative_w11_24h2
						<= moduleInfo.SizeOfImage - i && !aob_match_inverse(
							address, COverlayContext_OverlaysEnabled_bytes_relative_w11_24h2,
							sizeof COverlayContext_OverlaysEnabled_bytes_relative_w11_24h2))
					{


						COverlayContext_OverlaysEnabled_orig = (COverlayContext_OverlaysEnabled_t*)get_relative_address(address, 1, 5);
					}
					if (COverlayContext_Present_orig_24h2 && COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2 &&
						COverlayContext_OverlaysEnabled_orig)
					{
						break;
					}
				}
			}
			else if (isWindows11)
			{
				for (size_t i = 0; i <= moduleInfo.SizeOfImage - sizeof COverlayContext_OverlaysEnabled_bytes_w11; i++)
				{
					unsigned char* address = (unsigned char*)dwmcore + i;
					if (!COverlayContext_Present_orig && sizeof COverlayContext_Present_bytes_w11 <= moduleInfo.
						SizeOfImage - i && !aob_match_inverse(address, COverlayContext_Present_bytes_w11,
						                                      sizeof COverlayContext_Present_bytes_w11))
					{
						COverlayContext_Present_orig = (COverlayContext_Present_t*)address;
						COverlayContext_Present_real_orig = COverlayContext_Present_orig;
					}
					else if (!COverlayContext_IsCandidateDirectFlipCompatbile_orig && sizeof
						COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11 <= moduleInfo.SizeOfImage - i && !
						aob_match_inverse(
							address, COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11,
							sizeof COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11))
					{
						COverlayContext_IsCandidateDirectFlipCompatbile_orig = (
							COverlayContext_IsCandidateDirectFlipCompatbile_t*)address;
					}
					else if (!COverlayContext_OverlaysEnabled_orig && sizeof COverlayContext_OverlaysEnabled_bytes_w11
						<= moduleInfo.SizeOfImage - i && !aob_match_inverse(
							address, COverlayContext_OverlaysEnabled_bytes_w11,
							sizeof COverlayContext_OverlaysEnabled_bytes_w11))
					{
						COverlayContext_OverlaysEnabled_orig = (COverlayContext_OverlaysEnabled_t*)address;
					}
					if (COverlayContext_Present_orig && COverlayContext_IsCandidateDirectFlipCompatbile_orig &&
						COverlayContext_OverlaysEnabled_orig)
					{
						break;
					}
				}

				DWORD rev;
				DWORD revSize = sizeof(rev);
				RegGetValueA(HKEY_LOCAL_MACHINE, "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "UBR", RRF_RT_DWORD,
				             NULL, &rev, &revSize);

				if (rev >= 706)
				{

				}
			}
			else
			{
				for (size_t i = 0; i <= moduleInfo.SizeOfImage - sizeof(COverlayContext_Present_bytes); i++)
				{
					unsigned char* address = (unsigned char*)dwmcore + i;
					if (!COverlayContext_Present_orig && !memcmp(address, COverlayContext_Present_bytes,
					                                             sizeof(COverlayContext_Present_bytes)))
					{
						COverlayContext_Present_orig = (COverlayContext_Present_t*)address;
						COverlayContext_Present_real_orig = COverlayContext_Present_orig;
					}
					else if (!COverlayContext_IsCandidateDirectFlipCompatbile_orig && !memcmp(
						address, COverlayContext_IsCandidateDirectFlipCompatbile_bytes,
						sizeof(COverlayContext_IsCandidateDirectFlipCompatbile_bytes)))
					{
						static int found = 0;
						found++;
						if (found == 2)
						{
							COverlayContext_IsCandidateDirectFlipCompatbile_orig = (
								COverlayContext_IsCandidateDirectFlipCompatbile_t*)(address - 0xa);
						}
					}
					else if (!COverlayContext_OverlaysEnabled_orig && !memcmp(
						address, COverlayContext_OverlaysEnabled_bytes, sizeof(COverlayContext_OverlaysEnabled_bytes)))
					{
						COverlayContext_OverlaysEnabled_orig = (COverlayContext_OverlaysEnabled_t*)(address - 0x7);
					}
					if (COverlayContext_Present_orig && COverlayContext_IsCandidateDirectFlipCompatbile_orig &&
						COverlayContext_OverlaysEnabled_orig)
					{
						break;
					}
				}
			}

			char lutFolderPath[MAX_PATH];
			ExpandEnvironmentStringsA(LUT_FOLDER, lutFolderPath, sizeof(lutFolderPath));
			if (!AddLUTs(lutFolderPath))
			{
				log_to_file("AddLUTs FAILED — returning FALSE");
				return FALSE;
			}
			{
				char msg[256];
				snprintf(msg, sizeof(msg), "AddLUTs OK: numLuts=%d hooks=%s",
					numLuts, (COverlayContext_Present_orig_24h2 ? "24h2" :
					          (COverlayContext_Present_orig ? "pre-24h2" : "none")));
				log_to_file(msg);
			}
			// Activate hooks if we have LUTs OR tonemap enabled via shared memory
			bool hasTonemapViaShared = false;
			for (int ti = 0; ti < g_numLocalTonemap; ti++) {
				if (g_localTonemap[ti].enabled) { hasTonemapViaShared = true; break; }
			}
			bool hasActiveWork = (numLuts > 0) || hasTonemapViaShared;

			if (hasActiveWork && (COverlayContext_Present_orig || COverlayContext_Present_orig_24h2))

			{
				if (MH_Initialize() != MH_OK) {
				log_to_file("ERROR: MH_Initialize failed");
				return FALSE;
			}
			MH_STATUS mhStatus;

				// Read hook level override before creating hooks.
				// 0=none, 1=Present, 2=+IsCandidateDirectFlip, 3=+fallback DirectFlip,
				// 4=+OverlayTestMode=5+OverlaysEnabled, 5=OverlaysEnabled via MinHook
				{
					char hlPath[MAX_PATH] = {0};
					ExpandEnvironmentStringsA(
						"%SYSTEMROOT%\\Temp\\DesktopLUT_HookLevel.flag",
						hlPath, sizeof(hlPath));
					FILE* hlf = fopen(hlPath, "r");
					if (hlf) {
						char buf[16] = {0};
						size_t n = fread(buf, 1, sizeof(buf) - 1, hlf);
						if (n >= 1 && buf[0] >= '0' && buf[0] <= '5') {
							g_hookLevel = buf[0] - '0';
							bool o = strchr(buf + 1, 'o') != NULL;
							bool d = strchr(buf + 1, 'd') != NULL;
							bool e = strchr(buf + 1, 'e') != NULL;
							g_diagNoLutDraw = strchr(buf + 1, 'n') != NULL;
							if (o || d || e) {
								g_l4OverlayTestMode = o;
								g_l4DisableIFlip = d;
								g_l4OverlaysEnabledForceTrue = e;
							}
						}
						fclose(hlf);
					}
					{
						DWORD v = 0, cb = sizeof(v);
						if (RegGetValueW(HKEY_LOCAL_MACHINE,
						        L"SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers",
						        L"HwSchMode", RRF_RT_REG_DWORD, NULL, &v, &cb) == ERROR_SUCCESS)
							g_hwSchMode = (int)v;
					}
					char msg[192];
					snprintf(msg, sizeof(msg),
						"DIAG: hookLevel=%d (0=none..5=all) l4: otm=%d diflip=%d ovEnForceTrue=%d noDraw=%d | HwSchMode=%d",
						g_hookLevel, g_l4OverlayTestMode ? 1 : 0, g_l4DisableIFlip ? 1 : 0,
						g_l4OverlaysEnabledForceTrue ? 1 : 0, g_diagNoLutDraw ? 1 : 0, g_hwSchMode);
					log_to_file(msg);
				}

				if (g_hookLevel >= 1) {
					if (!isWindows11_24h2 && !isWindows11_25h2)
						mhStatus = MH_CreateHook((PVOID)COverlayContext_Present_orig, (PVOID)COverlayContext_Present_hook,
									  (PVOID*)&COverlayContext_Present_orig);
					else
						mhStatus = MH_CreateHook((PVOID)COverlayContext_Present_orig_24h2, (PVOID)COverlayContext_Present_hook_24h2,
							(PVOID*)&COverlayContext_Present_orig_24h2);
					if (mhStatus != MH_OK) {
						log_to_file("ERROR: MH_CreateHook failed for COverlayContext::Present");
						MH_Uninitialize();
						return FALSE;
					}
					LOG_ONLY_ONCE("Hook L1: Present")
				}

				if (g_hookLevel >= 2) {
					if (!isWindows11_24h2 && !isWindows11_25h2)
						mhStatus = MH_CreateHook((PVOID)COverlayContext_IsCandidateDirectFlipCompatbile_orig,
									  (PVOID)COverlayContext_IsCandidateDirectFlipCompatbile_hook,
									  (PVOID*)&COverlayContext_IsCandidateDirectFlipCompatbile_orig);
					else
						mhStatus = MH_CreateHook((PVOID)COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2,
							(PVOID)COverlayContext_IsCandidateDirectFlipCompatbile_hook_24h2,
							(PVOID*)&COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2);
					if (mhStatus != MH_OK) {
						log_to_file("ERROR: MH_CreateHook failed for IsCandidateDirectFlipCompatible");
					}
					LOG_ONLY_ONCE("Hook L2: IsCandidateDirectFlipCompatible")
				}

				if (g_hookLevel >= 3) {
				if (CWindowContext_IsCandidateDirectFlipCompatbile_orig)
				{
					mhStatus = MH_CreateHook((PVOID)CWindowContext_IsCandidateDirectFlipCompatbile_orig,
						(PVOID)CWindowContext_IsCandidateDirectFlipCompatbile_hook,
						(PVOID*)&CWindowContext_IsCandidateDirectFlipCompatbile_orig);
					if (mhStatus == MH_OK) {
						LOG_ONLY_ONCE("Hooked CWindowContext::IsCandidateDirectFlipCompatible")
					} else {
						LOG_ONLY_ONCE("FAILED to hook CWindowContext::IsCandidateDirectFlipCompatible")
					}
				}
				else {
					LOG_ONLY_ONCE("FAILED to find CWindowContext::IsCandidateDirectFlipCompatible")
				}

				if (CCompSwapChain_IsCandidateIndependentFlipCompatible_orig)
				{
					mhStatus = MH_CreateHook((PVOID)CCompSwapChain_IsCandidateIndependentFlipCompatible_orig,
						(PVOID)CCompSwapChain_IsCandidateIndependentFlipCompatible_hook,
						(PVOID*)&CCompSwapChain_IsCandidateIndependentFlipCompatible_orig);
					if (mhStatus == MH_OK) {
						LOG_ONLY_ONCE("Hooked CCompSwapChain::IsCandidateIndependentFlipCompatible")
					} else {
						LOG_ONLY_ONCE("FAILED to hook CCompSwapChain::IsCandidateIndependentFlipCompatible")
					}
				}
				else {
					LOG_ONLY_ONCE("FAILED to find CCompSwapChain::IsCandidateIndependentFlipCompatible")
				}

				if (CCompSwapChain_IsCandidateDirectFlipCompatbile_orig)
				{
					mhStatus = MH_CreateHook((PVOID)CCompSwapChain_IsCandidateDirectFlipCompatbile_orig,
						(PVOID)CCompSwapChain_IsCandidateDirectFlipCompatbile_hook,
						(PVOID*)&CCompSwapChain_IsCandidateDirectFlipCompatbile_orig);
					if (mhStatus == MH_OK) {
						LOG_ONLY_ONCE("Hooked CCompSwapChain::IsCandidateDirectFlipCompatible")
					} else {
						LOG_ONLY_ONCE("FAILED to hook CCompSwapChain::IsCandidateDirectFlipCompatible")
					}
				}
				else {
					LOG_ONLY_ONCE("FAILED to find CCompSwapChain::IsCandidateDirectFlipCompatible")
				}

				if (CCompVisual_IsCandidateForPromotion_orig)
				{
					mhStatus = MH_CreateHook((PVOID)CCompVisual_IsCandidateForPromotion_orig,
						(PVOID)CCompVisual_IsCandidateForPromotion_hook,
						(PVOID*)&CCompVisual_IsCandidateForPromotion_orig);
					if (mhStatus == MH_OK) {
						LOG_ONLY_ONCE("Hooked CCompVisual::IsCandidateForPromotion")
					} else {
						LOG_ONLY_ONCE("FAILED to hook CCompVisual::IsCandidateForPromotion")
					}
				}
				else {
					LOG_ONLY_ONCE("FAILED to find CCompVisual::IsCandidateForPromotion")
				}
				} // g_hookLevel >= 3

				if (g_hookLevel < 3)
				{
					LOG_ONLY_ONCE("Hook L3: fallback DirectFlip hooks SKIPPED (hookLevel < 3)")
				}
				else
				{
					if (COverlayContext_IsDirectFlipSupportedOnTarget_orig)
					{
						mhStatus = MH_CreateHook((PVOID)COverlayContext_IsDirectFlipSupportedOnTarget_orig,
							(PVOID)COverlayContext_IsDirectFlipSupportedOnTarget_hook,
							(PVOID*)&COverlayContext_IsDirectFlipSupportedOnTarget_orig);
						if (mhStatus == MH_OK) {
							LOG_ONLY_ONCE("Hooked COverlayContext::IsDirectFlipSupportedOnTarget")
						} else {
							LOG_ONLY_ONCE("FAILED to hook COverlayContext::IsDirectFlipSupportedOnTarget")
						}
					}
					else {
						LOG_ONLY_ONCE("FAILED to find COverlayContext::IsDirectFlipSupportedOnTarget")
					}

					if (CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_orig)
					{
						mhStatus = MH_CreateHook((PVOID)CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_orig,
							(PVOID)CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_hook,
							(PVOID*)&CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_orig);
						if (mhStatus == MH_OK) {
							LOG_ONLY_ONCE("Hooked CGlobalCompositionSurfaceInfo::IsAdvancedDirectFlipCompatible")
						} else {
							LOG_ONLY_ONCE("FAILED to hook CGlobalCompositionSurfaceInfo::IsAdvancedDirectFlipCompatible")
						}
					}
					else {
						LOG_ONLY_ONCE("FAILED to find CGlobalCompositionSurfaceInfo::IsAdvancedDirectFlipCompatible")
					}
				}

				if (g_pOverlayTestMode != NULL && g_hookLevel >= 4 && g_l4OverlayTestMode)
				{
					__try { g_savedOverlayTestMode = *g_pOverlayTestMode; *g_pOverlayTestMode = 5; g_overlayTestModePatched = true; }
					__except (EXCEPTION_EXECUTE_HANDLER) { g_pOverlayTestMode = NULL; }
					LOG_ONLY_ONCE("Forced OverlayTestMode to 5")
				}
				else if (g_pOverlayTestMode == NULL) {
					LOG_ONLY_ONCE("WARNING: g_pOverlayTestMode not found")
				}

				// DisableIndependentFlip: 25H2-only (replaces 4 removed DirectFlip functions).
				// Validate page is writable before patching.
				if (g_pDisableIndependentFlip != NULL && isWindows11_25h2 && g_hookLevel >= 4 && g_l4DisableIFlip)
				{
					MEMORY_BASIC_INFORMATION mbi = {};
					if (VirtualQuery(g_pDisableIndependentFlip, &mbi, sizeof(mbi)) &&
					    (mbi.Protect & (PAGE_READWRITE | PAGE_EXECUTE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_WRITECOPY)))
					{
						__try { g_savedDisableIndependentFlip = *g_pDisableIndependentFlip; *g_pDisableIndependentFlip = 1; g_disableIndependentFlipPatched = true; }
						__except (EXCEPTION_EXECUTE_HANDLER) { g_pDisableIndependentFlip = NULL; }
						LOG_ONLY_ONCE("Forced DisableIndependentFlip = 1")
					} else {
						g_pDisableIndependentFlip = NULL;
						LOG_ONLY_ONCE("WARNING: DisableIndependentFlip page not writable — skipped")
					}
				}

				// OverlaysEnabled hook: on 25H2+ use inline-patch (MinHook trampoline
				// crashes on dwmcore.dll >= 26200.8457). On older builds, use MinHook.
				if (g_hookLevel >= 5) {
					// Level 5: force MinHook (diagnostic — known to crash on 25H2+)
					if (COverlayContext_OverlaysEnabled_orig) {
						MH_CreateHook((PVOID)COverlayContext_OverlaysEnabled_orig, (PVOID)COverlayContext_OverlaysEnabled_hook,
						              (PVOID*)&COverlayContext_OverlaysEnabled_orig);
						LOG_ONLY_ONCE("Hook L5: OverlaysEnabled (MinHook)")
					}
				}
				else if (isWindows11_25h2 && g_hookLevel >= 4 && g_disableIndependentFlipPatched && !g_l4OverlaysEnabledForceTrue)
				{
					// Opt-in "4od": OverlaysEnabled left original. On 25H2 this lets full-monitor windows
					// bypass DWM composition — the LUT no longer applies to them, but they get
					// Independent-Flip-class pacing under HAGS. NOT production (see the
					// g_l4OverlaysEnabledForceTrue comment); only for clients that apply the cube themselves.
					LOG_ONLY_ONCE("OverlaysEnabled left ORIGINAL (opt-in '4od': full-monitor windows bypass DWM — no LUT on them)")
				}
				else if (isWindows11_25h2 && g_hookLevel >= 4 && COverlayContext_OverlaysEnabled_orig != NULL)
				{
					// 25H2 production: inline-patch OverlaysEnabled. MinHook trampoline crashes on KB5089549+.
					// If DisableIndependentFlip was patched: return TRUE (iFlip suppressed globally;
					// full-monitor windows stay composed so the LUT covers them).
					// If not: return FALSE (fail-safe: force composition, same effect as the
					// pre-25H2 MinHook hook).
					unsigned char* func = (unsigned char*)COverlayContext_OverlaysEnabled_orig;
					if (func[0] == 0x83 && func[1] == 0x3D && func[6] == 0x05 &&
					    func[7] == 0x74 && func[8] == 0x09)
					{
						DWORD oldProt;
						if (VirtualProtect(func, 17, PAGE_EXECUTE_READWRITE, &oldProt)) {
							memcpy(g_overlaysEnabledOrigBytes, func, 17);
							g_overlaysEnabledPatchAddr = func;
							func[0] = 0xB0; func[1] = g_disableIndependentFlipPatched ? 0x01 : 0x00;
							func[2] = 0xC3;                   // ret
							for (int p = 3; p < 17; p++) func[p] = 0xCC;  // int3 padding
							VirtualProtect(func, 17, oldProt, &oldProt);
							FlushInstructionCache(GetCurrentProcess(), func, 17);
							LOG_ONLY_ONCE(g_disableIndependentFlipPatched
						? "OverlaysEnabled inline-patched (mov al,1; ret)"
						: "OverlaysEnabled inline-patched (mov al,0; ret) — DisableIndependentFlip unavailable")
						}
					}
				}
				else if (!isWindows11_25h2 && g_hookLevel >= 4 && COverlayContext_OverlaysEnabled_orig != NULL)
				{
					// Pre-25H2: MinHook works fine on older OverlaysEnabled functions
					MH_CreateHook((PVOID)COverlayContext_OverlaysEnabled_orig, (PVOID)COverlayContext_OverlaysEnabled_hook,
					              (PVOID*)&COverlayContext_OverlaysEnabled_orig);
					LOG_ONLY_ONCE("Hooked OverlaysEnabled (MinHook)")
				}

				if (g_hookLevel == 0) {
					LOG_ONLY_ONCE("DIAG: hookLevel=0 — no hooks activated, memory patches only")
					MH_Uninitialize();
					break;  // Inert mode: skip heartbeat + host monitor
				} else if (MH_EnableHook(MH_ALL_HOOKS) != MH_OK) {
					log_to_file("ERROR: MH_EnableHook failed");
					MH_Uninitialize();
					return FALSE;
				}
				LOG_ONLY_ONCE("DWM HOOK DLL INITIALIZATION. START LOGGING")

				// Create heartbeat event so host can detect hook is active
				{
					SECURITY_DESCRIPTOR sd;
					InitializeSecurityDescriptor(&sd, SECURITY_DESCRIPTOR_REVISION);
					SetSecurityDescriptorDacl(&sd, TRUE, NULL, FALSE);  // NULL DACL = everyone can access
					SECURITY_ATTRIBUTES sa = { sizeof(sa), &sd, FALSE };
					g_heartbeatEvent = CreateEventW(&sa, TRUE, TRUE, L"Global\\DesktopLUT_DwmHook_Active");
					if (g_heartbeatEvent)
						log_to_file("Heartbeat event created: Global\\DesktopLUT_DwmHook_Active");
					else
						log_to_file("WARNING: Failed to create heartbeat event");
				}

				// Start host process monitor (detects orphaned injection)
				// dwm.exe is a Protected Process — can't OpenProcess on the host.
				// Uses process list polling (CreateToolhelp32Snapshot) every 5s instead.
				{
					g_hostPid = ReadHostPid();
					if (g_hostPid != 0) {
						g_hostMonitorStopEvent = CreateEvent(NULL, TRUE, FALSE, NULL);
						if (g_hostMonitorStopEvent) {
							g_hostMonitorThread = CreateThread(NULL, 0, HostMonitorThreadFunc, NULL, 0, NULL);
							if (g_hostMonitorThread) {
								char buf[128];
								sprintf_s(buf, "Host monitor started for PID %lu (polling)", g_hostPid);
								log_to_file(buf);
							} else {
								log_to_file("WARNING: Failed to create host monitor thread");
							}
						}
					} else {
						log_to_file("WARNING: No host.pid found — orphan detection disabled");
					}
				}

				break;
			}
			log_to_file("Hook condition not met — returning FALSE");
			return FALSE;
		}
	case DLL_PROCESS_DETACH:
		// lpReserved != NULL => the process (dwm.exe) is terminating, not an explicit
		// FreeLibrary uninject. On process exit the OS reclaims all memory, handles, and
		// the patched code pages, so the blocking thread-join + D3D teardown + free() below
		// are not only unnecessary but actively dangerous under the loader lock (a deadlock
		// if the monitored thread is blocked needing the lock). Skip all cleanup; only do
		// the full teardown on a real FreeLibrary unload (lpReserved == NULL, the host
		// uninject path). Matches dwm_hook/CLAUDE.md: "never block under the loader lock".
		if (lpReserved != NULL) {
			break;
		}
		log_to_file("DLL_PROCESS_DETACH: cleanup starting");

		// Stop host monitor thread first (before removing hooks)
		if (g_hostMonitorStopEvent) {
			SetEvent(g_hostMonitorStopEvent);
		}
		if (g_hostMonitorThread) {
			// Wait briefly for thread exit — stop event was signaled above,
			// thread wakes from WaitForSingleObject immediately. 200ms is generous.
			DWORD waitResult = WaitForSingleObject(g_hostMonitorThread, 200);
			if (waitResult == WAIT_TIMEOUT) {
				log_to_file("WARNING: Host monitor thread still running after 200ms — closing handle");
			}
			CloseHandle(g_hostMonitorThread);
			g_hostMonitorThread = NULL;
		}
		if (g_hostMonitorStopEvent) { CloseHandle(g_hostMonitorStopEvent); g_hostMonitorStopEvent = NULL; }

		// Close heartbeat event (host will see hook as gone)
		if (g_heartbeatEvent) { CloseHandle(g_heartbeatEvent); g_heartbeatEvent = NULL; }

		// Only revert hooks/overlay mode if not already done by host monitor
		if (!g_hookReverted.exchange(true)) {
			if (g_overlayTestModePatched && g_pOverlayTestMode != NULL)
			{
				__try { *g_pOverlayTestMode = g_savedOverlayTestMode; }
				__except (EXCEPTION_EXECUTE_HANDLER) { /* already detaching, nothing to do */ }
			}
			if (g_disableIndependentFlipPatched && g_pDisableIndependentFlip != NULL)
			{
				__try { *g_pDisableIndependentFlip = g_savedDisableIndependentFlip; }
				__except (EXCEPTION_EXECUTE_HANDLER) {}
			}
			if (g_overlaysEnabledPatchAddr != NULL)
			{
				DWORD oldProt;
				if (VirtualProtect(g_overlaysEnabledPatchAddr, 17, PAGE_EXECUTE_READWRITE, &oldProt)) {
					memcpy(g_overlaysEnabledPatchAddr, g_overlaysEnabledOrigBytes, 17);
					VirtualProtect(g_overlaysEnabledPatchAddr, 17, oldProt, &oldProt);
					FlushInstructionCache(GetCurrentProcess(), g_overlaysEnabledPatchAddr, 17);
				}
				g_overlaysEnabledPatchAddr = NULL;
			}
			MH_DisableHook(MH_ALL_HOOKS);
			MH_Uninitialize();
		}
		// Close shared memory
		if (g_sharedConfig) { UnmapViewOfFile((void*)g_sharedConfig); g_sharedConfig = NULL; }
		if (g_sharedMemHandle) { CloseHandle(g_sharedMemHandle); g_sharedMemHandle = NULL; }

		UninitializeStuff();
		log_to_file("DLL_PROCESS_DETACH: cleanup complete");
		break;
	default:
		break;
	}
	return TRUE;
}
