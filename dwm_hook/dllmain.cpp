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
#include <atomic>
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

			if (g_pOverlayTestMode != NULL) {
				__try { *g_pOverlayTestMode = 0; }
				__except (EXCEPTION_EXECUTE_HANDLER) {}
			}

			MH_DisableHook(MH_ALL_HOOKS);
			g_hookReverted = true;

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


static void UpdateLocalTonemapFromShared() {
	if (!g_sharedConfig) return;

	uint32_t v1 = g_sharedConfig->version;
	if (v1 == g_localConfigVersion) return;

	// Acquire fence pairs with release fence on the host write side,
	// ensuring all config fields are visible after the version check.
	std::atomic_thread_fence(std::memory_order_acquire);

	DwmHookSharedConfig local;
	memcpy(&local, (const void*)g_sharedConfig, sizeof(local));

	// Seqlock: re-read version after copy to detect torn reads.
	// If the host was mid-write during our memcpy, versions won't match.
	// Skip this frame — next Present call will pick up the consistent state.
	std::atomic_thread_fence(std::memory_order_acquire);
	uint32_t v2 = g_sharedConfig->version;
	if (v1 != v2) return;

	g_localConfigVersion = local.version;

	// Clamp numMonitors to prevent OOB from shared memory
	uint32_t numMons = (local.numMonitors < MAX_DWM_HOOK_MONITORS) ? local.numMonitors : MAX_DWM_HOOK_MONITORS;

	// Update monitor HDR states from shared memory
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

	// Update local tonemap params
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
						}
						else
						{
							g_pOverlayTestMode = NULL;
							LOG_ONLY_ONCE("WARNING: g_pOverlayTestMode address outside dwmcore.dll range — skipped");
						}



						const unsigned char flipMatch[] = { 0x48, 0x8D, 0x05 };
						unsigned char* imageEnd = (unsigned char*)dwmcore + moduleInfo.SizeOfImage;
						for (int j = 0; j < 500; j++) {
							unsigned char* fAddr = address + j;
							if (fAddr + sizeof(flipMatch) > imageEnd) break;
							if (!memcmp(fAddr, flipMatch, sizeof(flipMatch))) {
								CCompSwapChain_IsCandidateIndependentFlipCompatible_orig = (CCompSwapChain_IsCandidateIndependentFlipCompatible_t*)fAddr;
								break;
							}
						}
					}
					if (COverlayContext_Present_orig_24h2 && COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2 &&
						COverlayContext_OverlaysEnabled_orig)
					{
						break;
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

			if (hasActiveWork && ((COverlayContext_Present_orig && COverlayContext_IsCandidateDirectFlipCompatbile_orig &&
				COverlayContext_OverlaysEnabled_orig) ||
				(COverlayContext_Present_orig_24h2 && COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2 && COverlayContext_OverlaysEnabled_orig)))

			{
				if (MH_Initialize() != MH_OK) {
				log_to_file("ERROR: MH_Initialize failed");
				return FALSE;
			}
			MH_STATUS mhStatus;
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

				if (g_pOverlayTestMode != NULL)
				{
					__try { *g_pOverlayTestMode = 5; }
					__except (EXCEPTION_EXECUTE_HANDLER) { g_pOverlayTestMode = NULL; }
					LOG_ONLY_ONCE("SUCCESS: Forced OverlayTestMode to 5")
				}
				else {
					LOG_ONLY_ONCE("FAILED to find g_pOverlayTestMode")
				}

				MH_CreateHook((PVOID)COverlayContext_OverlaysEnabled_orig, (PVOID)COverlayContext_OverlaysEnabled_hook,
				              (PVOID*)&COverlayContext_OverlaysEnabled_orig);
				if (MH_EnableHook(MH_ALL_HOOKS) != MH_OK) {
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
		if (!g_hookReverted) {
			if (g_pOverlayTestMode != NULL)
			{
				__try { *g_pOverlayTestMode = 0; }
				__except (EXCEPTION_EXECUTE_HANDLER) { /* already detaching, nothing to do */ }
			}
			MH_DisableHook(MH_ALL_HOOKS);
			MH_Uninitialize();
		}
		g_hookReverted = true;  // Prevent any late HostMonitor action after timeout
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
