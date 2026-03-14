/*
 * Copyright (C) 2021 ledoge
 * Modifications Copyright (C) 2026 Eduu
 *
 * This program is free software: you can redistribute it and/or modify...
 */
#include "pch.h"

#include <io.h>
#include <string>
#include <sstream>
#include <iostream>
#include <iomanip>
#pragma comment (lib, "d3d11.lib")
#pragma comment (lib, "d3dcompiler.lib")
#pragma comment (lib, "dxgi.lib")
#pragma comment (lib, "uuid.lib")
#pragma comment (lib, "dxguid.lib")


#pragma intrinsic(_ReturnAddress)

#define DITHER_GAMMA 2.2
#define LUT_FOLDER "%SYSTEMROOT%\\Temp\\DesktopLUT_luts"

#define RELEASE_IF_NOT_NULL(x) { if (x != NULL) { x->Release(); } }
#define _STRINGIFY(x) #x
#define STRINGIFY(x) _STRINGIFY(x)
#define MAX_LOG_FILE_SIZE (20 * 1024 * 1024)

// Always-on logging
static char g_logFilePath[MAX_PATH] = {0};

static const char* GetLogFilePath()
{
	if (g_logFilePath[0] == '\0')
	{
		ExpandEnvironmentStringsA("%SYSTEMROOT%\\Temp\\DesktopLUT_dwmhook.log", g_logFilePath, sizeof(g_logFilePath));
	}
	return g_logFilePath;
}

#define __LOG_ONLY_ONCE(x, y) if (static bool first_log_##y = true) { log_to_file(x); first_log_##y = false; }
#define _LOG_ONLY_ONCE(x, y) __LOG_ONLY_ONCE(x, y)
#define LOG_ONLY_ONCE(x) _LOG_ONLY_ONCE(x, __COUNTER__)

#define EXECUTE_WITH_LOG(winapi_func_hr) \
	do { \
		HRESULT hr = (winapi_func_hr); \
		if (FAILED(hr)) \
		{ \
			std::stringstream ss; \
			ss << "ERROR AT LINE: " << __LINE__ << " HR: " << hr << " - DETAILS: "; \
			LPSTR error_message = nullptr; \
			DWORD fmtResult = FormatMessageA(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, \
				NULL, hr, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT), (LPSTR)&error_message, 0, NULL); \
			ss << ((fmtResult > 0 && error_message) ? error_message : "(FormatMessage failed)"); \
			if (error_message) LocalFree(error_message); \
			log_to_file(ss.str().c_str()); \
			throw std::exception(ss.str().c_str()); \
		} \
	} while (false);

#define EXECUTE_D3DCOMPILE_WITH_LOG(winapi_func_hr, error_interface) \
	do { \
		HRESULT hr = (winapi_func_hr); \
		if (FAILED(hr)) \
		{ \
			std::stringstream ss; \
			ss << "ERROR AT LINE: " << __LINE__ << " HR: " << hr << " - DETAILS: "; \
			LPSTR error_message = nullptr; \
			DWORD fmtResult = FormatMessageA(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, \
				NULL, hr, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT), (LPSTR)&error_message, 0, NULL); \
			ss << ((fmtResult > 0 && error_message) ? error_message : "(FormatMessage failed)"); \
			ss << " - DX COMPILE ERROR: " << (char*)error_interface->GetBufferPointer(); \
			error_interface->Release(); \
			if (error_message) LocalFree(error_message); \
			log_to_file(ss.str().c_str()); \
			throw std::exception(ss.str().c_str()); \
		} \
	} while (false);

#define LOG_ADDRESS(prefix_message, address) \
	{ \
		std::stringstream ss; \
		ss << prefix_message << " 0x" << std::setw(sizeof(address) * 2) << std::setfill('0') << std::hex << (UINT_PTR)address; \
		log_to_file(ss.str().c_str()); \
	}


void log_to_file(const char* log_buf)
{
	FILE* pFile = fopen(GetLogFilePath(), "a");
	if (pFile == NULL)
	{
		return;
	}
	fseek(pFile, 0, SEEK_END);
	long size = ftell(pFile);
	if (size > MAX_LOG_FILE_SIZE)
	{
		if (_chsize(_fileno(pFile), 0) == -1)
		{
			fclose(pFile);
			return;
		}
	}
	fseek(pFile, 0, SEEK_END);
	fprintf(pFile, "%s\n", log_buf);
	fclose(pFile);
}


unsigned int lut_index(const unsigned int b, const unsigned int g, const unsigned int r, const unsigned int c,
                       const unsigned int lut_size)
{
	return lut_size * lut_size * 4 * b + lut_size * 4 * g + 4 * r + c;
}

#define LUT_ACCESS_INDEX(lut, b, g, r, c, lut_size) (*((float*)(lut) + lut_index(b, g, r, c, lut_size)))


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

const int COverlayContext_DeviceClipBox_offset = -0x120;

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
static volatile bool g_hookReverted = false;  // Set when host dies, prevents double-revert in DLL_PROCESS_DETACH

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

char shaders[] = R"(
    struct VS_INPUT {
	float2 pos : POSITION;
	float2 tex : TEXCOORD;
};

struct VS_OUTPUT {
	float4 pos : SV_POSITION;
	float2 tex : TEXCOORD;
};

Texture2D backBufferTex : register(t0);
Texture3D lutTex : register(t1);
SamplerState smp : register(s0);

Texture2D noiseTex : register(t2);
SamplerState noiseSmp : register(s1);

cbuffer Constants : register(b0) {
	int lutSize;
	int colorMode;  // 0=SDR 8-bit, 1=HDR, 2=ACM SDR (FP16 linear)
	int ditherLevels;
	int pad0;
};

static float3x3 scrgb_to_bt2100 = {
2939026994.L / 585553224375.L, 9255011753.L / 3513319346250.L,   173911579.L / 501902763750.L,
  76515593.L / 138420033750.L, 6109575001.L / 830520202500.L,    75493061.L / 830520202500.L,
  12225392.L / 93230009375.L, 1772384008.L / 2517210253125.L, 18035212433.L / 2517210253125.L,
};

static float3x3 bt2100_to_scrgb = {
 348196442125.L / 1677558947.L, -123225331250.L / 1677558947.L,  -15276242500.L / 1677558947.L,
-579752563250.L / 37238079773.L, 5273377093000.L / 37238079773.L,  -38864558125.L / 37238079773.L,
 -12183628000.L / 5369968309.L, -472592308000.L / 37589778163.L, 5256599974375.L / 37589778163.L,
};

static float m1 = 1305 / 8192.;
static float m2 = 2523 / 32.;
static float c1 = 107 / 128.;
static float c2 = 2413 / 128.;
static float c3 = 2392 / 128.;

float3 SampleLut(float3 index) {
	float3 tex = (index + 0.5) / lutSize;
	return lutTex.Sample(smp, tex).rgb;
}


void barycentricWeight(float3 r, out float4 bary, out int3 vert2, out int3 vert3) {
	vert2 = int3(0, 0, 0); vert3 = int3(1, 1, 1);
	int3 c = r.xyz >= r.yzx;
	bool c_xy = c.x; bool c_yz = c.y; bool c_zx = c.z;
	bool c_yx = !c.x; bool c_zy = !c.y; bool c_xz = !c.z;
	bool cond;  float3 s = float3(0, 0, 0);
#define ORDER(X, Y, Z)                   \
            cond = c_ ## X ## Y && c_ ## Y ## Z; \
            s = cond ? r.X ## Y ## Z : s;        \
            vert2.X = cond ? 1 : vert2.X;        \
            vert3.Z = cond ? 0 : vert3.Z;
	ORDER(x, y, z)   ORDER(x, z, y)   ORDER(z, x, y)
		ORDER(z, y, x)   ORDER(y, z, x)   ORDER(y, x, z)
		bary = float4(1 - s.x, s.z, s.x - s.y, s.y - s.z);
}

float3 LutTransformTetrahedral(float3 rgb) {
	float3 lutIndex = rgb * (lutSize - 1);
	float4 bary; int3 vert2; int3 vert3;
	barycentricWeight(frac(lutIndex), bary, vert2, vert3);

	float3 base = floor(lutIndex);
	return bary.x * SampleLut(base) +
		bary.y * SampleLut(base + 1) +
		bary.z * SampleLut(base + vert2) +
		bary.w * SampleLut(base + vert3);
}

float3 pq_eotf(float3 e) {
	return pow(max((pow(e, 1 / m2) - c1), 0) / (c2 - c3 * pow(e, 1 / m2)), 1 / m1);
}

float3 pq_inv_eotf(float3 y) {
	return pow((c1 + c2 * pow(y, m1)) / (1 + c3 * pow(y, m1)), m2);
}

float3 OrderedDither(float3 rgb, float2 pos) {
	float3 low = floor(rgb * ditherLevels) / ditherLevels;
	float3 high = low + 1.0 / ditherLevels;

	float3 rgb_linear = pow(rgb,)" STRINGIFY(DITHER_GAMMA) R"();
	float3 low_linear = pow(low,)" STRINGIFY(DITHER_GAMMA) R"();
	float3 high_linear = pow(high,)" STRINGIFY(DITHER_GAMMA) R"();

	float noise = noiseTex.Sample(noiseSmp, pos / )" STRINGIFY(NOISE_SIZE) R"().x;
	float3 threshold = lerp(low_linear, high_linear, noise);

	return lerp(low, high, rgb_linear > threshold);
}

VS_OUTPUT VS(VS_INPUT input) {
	VS_OUTPUT output;
	output.pos = float4(input.pos, 0, 1);
	output.tex = input.tex;
	return output;
}

float3 srgb_encode(float3 L) {
	return float3(
		L.r <= 0.0031308 ? 12.92 * L.r : 1.055 * pow(L.r, 1.0/2.4) - 0.055,
		L.g <= 0.0031308 ? 12.92 * L.g : 1.055 * pow(L.g, 1.0/2.4) - 0.055,
		L.b <= 0.0031308 ? 12.92 * L.b : 1.055 * pow(L.b, 1.0/2.4) - 0.055);
}

float3 srgb_decode(float3 V) {
	return float3(
		V.r <= 0.04045 ? V.r / 12.92 : pow((V.r + 0.055) / 1.055, 2.4),
		V.g <= 0.04045 ? V.g / 12.92 : pow((V.g + 0.055) / 1.055, 2.4),
		V.b <= 0.04045 ? V.b / 12.92 : pow((V.b + 0.055) / 1.055, 2.4));
}

float4 PS(VS_OUTPUT input) : SV_TARGET{
	float3 sample = backBufferTex.Sample(smp, input.tex).rgb;

	if (colorMode == 1) {
		// HDR: scRGB linear -> BT.2100 PQ -> LUT -> PQ decode -> scRGB
		float3 hdr10_sample = pq_inv_eotf(saturate(mul(scrgb_to_bt2100, sample)));

		float3 hdr10_res = LutTransformTetrahedral(hdr10_sample);

		float3 scrgb_res = mul(bt2100_to_scrgb, pq_eotf(hdr10_res));

		return float4(scrgb_res, 1);
	}
	else if (colorMode == 2) {
		// ACM SDR: scRGB linear [0,1] -> sRGB encode -> LUT -> sRGB decode -> scRGB linear
		float3 srgb = srgb_encode(saturate(sample));

		float3 res = LutTransformTetrahedral(srgb);

		return float4(srgb_decode(res), 1);
	}
	else {
		// Legacy SDR: 8-bit sRGB gamma input -> LUT -> dither
		float3 res = LutTransformTetrahedral(sample);

		res = OrderedDither(res, input.pos.xy);

		return float4(res, 1);
	}
}
)";

ID3D11Device* device;
ID3D11DeviceContext* deviceContext;
ID3D11VertexShader* vertexShader;
ID3D11PixelShader* pixelShader;
ID3D11InputLayout* inputLayout;

ID3D11Buffer* vertexBuffer;
UINT numVerts;
UINT stride;
UINT offset;

D3D11_TEXTURE2D_DESC backBufferDesc;
D3D11_TEXTURE2D_DESC textureDesc[2];

ID3D11SamplerState* samplerState;
ID3D11Texture2D* texture[2];
ID3D11ShaderResourceView* textureView[2];

ID3D11SamplerState* noiseSamplerState;
ID3D11ShaderResourceView* noiseTextureView;

ID3D11Buffer* constantBuffer;

// Per-monitor HDR state detected via DXGI output enumeration
struct MonitorHdrState {
	int left, top;
	bool isHdr;  // true = HDR (PQ), false = SDR (sRGB) or ACM SDR (linear sRGB)
	UINT bpc;    // BitsPerColor from DXGI_OUTPUT_DESC1
	UINT width, height;  // resolution for matching
};
static MonitorHdrState g_monitorHdrStates[16];
static int g_numMonitorHdrStates = 0;
static bool g_hdrStatesDetected = false;

static bool IsMonitorHdr(int left, int top) {
	for (int i = 0; i < g_numMonitorHdrStates; i++) {
		if (g_monitorHdrStates[i].left == left && g_monitorHdrStates[i].top == top)
			return g_monitorHdrStates[i].isHdr;
	}
	return false;  // default to SDR if unknown (safer — avoids PQ pipeline on SDR content)
}

struct lutData
{
	int left;
	int top;
	int size;
	bool isHdr;
	ID3D11ShaderResourceView* textureView;
	float* rawLut;
};

void DrawRectangle(struct tagRECT* rect, int index)
{
	float width = backBufferDesc.Width;
	float height = backBufferDesc.Height;

	float screenLeft = rect->left / width;
	float screenTop = rect->top / height;
	float screenRight = rect->right / width;
	float screenBottom = rect->bottom / height;

	float left = screenLeft * 2 - 1;
	float top = screenTop * -2 + 1;
	float right = screenRight * 2 - 1;
	float bottom = screenBottom * -2 + 1;

	width = textureDesc[index].Width;
	height = textureDesc[index].Height;
	float texLeft = rect->left / width;
	float texTop = rect->top / height;
	float texRight = rect->right / width;
	float texBottom = rect->bottom / height;

	float vertexData[] = {
		left, bottom, texLeft, texBottom,
		left, top, texLeft, texTop,
		right, bottom, texRight, texBottom,
		right, top, texRight, texTop
	};

	D3D11_MAPPED_SUBRESOURCE resource;
	EXECUTE_WITH_LOG(deviceContext->Map(vertexBuffer, 0, D3D11_MAP_WRITE_DISCARD, 0, &resource))
	memcpy(resource.pData, vertexData, stride * numVerts);
	deviceContext->Unmap(vertexBuffer, 0);

	deviceContext->IASetVertexBuffers(0, 1, &vertexBuffer, &stride, &offset);

	deviceContext->Draw(numVerts, 0);
}

int numLuts;

lutData* luts;

bool ParseLUT(lutData* lut, char* filename)
{
	FILE* file = fopen(filename, "r");
	if (file == NULL) return false;

	char line[256];
	unsigned int lutSize;

	while (1)
	{
		if (!fgets(line, sizeof(line), file))
		{
			fclose(file);
			return false;
		}
		if (sscanf(line, "LUT_3D_SIZE %d", &lutSize) == 1)
		{
			if (lutSize < 2 || lutSize > 128)
			{
				fclose(file);
				return false;
			}
			break;
		}
	}

	float* rawLut = (float*)malloc((size_t)lutSize * lutSize * lutSize * 4 * sizeof(float));
	if (!rawLut)
	{
		fclose(file);
		return false;
	}


	for (unsigned int b = 0; b < lutSize; b++)
	{
		for (unsigned int g = 0; g < lutSize; g++)
		{
			for (unsigned int r = 0; r < lutSize; r++)
			{
				while (1)
				{
					if (!fgets(line, sizeof(line), file))
					{
						fclose(file);
						free(rawLut);
						return false;
					}
					if (((line[0] >= '0' && line[0] <= '9') || line[0] == '-' || line[0] == '+' || line[0] == '.') && line[0] != '#' && line[0] != '\n')
					{
						float red, green, blue;

						if (sscanf(line, "%f%f%f", &red, &green, &blue) != 3)
						{
							fclose(file);
							free(rawLut);
							return false;
						}
						LUT_ACCESS_INDEX(rawLut, b, g, r, 0, lutSize) = red;
						LUT_ACCESS_INDEX(rawLut, b, g, r, 1, lutSize) = green;
						LUT_ACCESS_INDEX(rawLut, b, g, r, 2, lutSize) = blue;
						LUT_ACCESS_INDEX(rawLut, b, g, r, 3, lutSize) = 1;

						break;
					}
				}
			}
		}
	}
	fclose(file);
	lut->size = lutSize;
	lut->rawLut = rawLut;
	return true;
}

bool AddLUTs(char* folder)
{
	WIN32_FIND_DATAA findData;

	char path[MAX_PATH];
	snprintf(path, sizeof(path), "%s\\*", folder);
	HANDLE hFind = FindFirstFileA(path, &findData);
	if (hFind == INVALID_HANDLE_VALUE) return false;
	do
	{
		if (!(findData.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
		{
			char filePath[MAX_PATH];
			char* fileName = findData.cFileName;

			snprintf(filePath, sizeof(filePath), "%s\\%s", folder, fileName);

			lutData* tmp = (lutData*)realloc(luts, (size_t)(numLuts + 1) * sizeof(lutData));
			if (!tmp)
			{
				FindClose(hFind);
				return false;
			}
			luts = tmp;
			lutData* lut = &luts[numLuts];
			if (sscanf(findData.cFileName, "%d_%d", &lut->left, &lut->top) == 2)
			{
				lut->isHdr = strstr(fileName, "hdr") != NULL;
				lut->textureView = NULL;
				if (!ParseLUT(lut, filePath))
				{
					LOG_ONLY_ONCE("LUT could not be parsed");
					FindClose(hFind);
					return false;
				}
				numLuts++;
			}
		}
	}
	while (FindNextFileA(hFind, &findData) != 0);
	FindClose(hFind);
	return true;
}

int numLutTargets;
void** lutTargets;



static void* g_primaryHdrContext = NULL;

bool IsLUTActive(void* target)
{
	for (int i = 0; i < numLutTargets; i++)
	{
		if (lutTargets[i] == target)
		{
			return true;
		}
	}
	return false;
}

void SetLUTActive(void* target)
{
	if (!IsLUTActive(target))
	{
		void** tmp = (void**)realloc(lutTargets, (size_t)(numLutTargets + 1) * sizeof(void*));
		if (!tmp) return;
		lutTargets = tmp;
		lutTargets[numLutTargets++] = target;
	}
}

void UnsetLUTActive(void* target)
{
	for (int i = 0; i < numLutTargets; i++)
	{
		if (lutTargets[i] == target)
		{
			lutTargets[i] = lutTargets[--numLutTargets];
			if (numLutTargets > 0)
			{
				void** tmp = (void**)realloc(lutTargets, (size_t)numLutTargets * sizeof(void*));
				if (tmp) lutTargets = tmp;
			}
			else
			{
				free(lutTargets);
				lutTargets = NULL;
			}
			return;
		}
	}
}

// Cache: map cOverlayContext pointers to monitor positions (populated via swapchain in ApplyLUT)
struct ContextPositionCache {
	void* context;
	int left, top;
};
static ContextPositionCache g_contextPosCache[16] = {};
static int g_numContextPosCache = 0;

static void CacheContextPosition(void* context, int left, int top) {
	for (int i = 0; i < g_numContextPosCache; i++) {
		if (g_contextPosCache[i].context == context) {
			g_contextPosCache[i].left = left;
			g_contextPosCache[i].top = top;
			return;
		}
	}
	if (g_numContextPosCache < 16) {
		g_contextPosCache[g_numContextPosCache++] = { context, left, top };
	}
}

static bool LookupContextPosition(void* context, int& left, int& top) {
	for (int i = 0; i < g_numContextPosCache; i++) {
		if (g_contextPosCache[i].context == context) {
			left = g_contextPosCache[i].left;
			top = g_contextPosCache[i].top;
			return true;
		}
	}
	return false;
}

// Extract monitor desktop position from COverlayContext
static void GetMonitorPositionFromContext(void* context, int& left, int& top)
{
	__try
	{
		if (isWindows11_25h2)
		{
			// 25H2: Use cached position from swapchain GetContainingOutput (set in ApplyLUT)
			if (LookupContextPosition(context, left, top))
				return;
			// Fallback if not yet cached
			left = 0;
			top = 0;
		}
		else if (isWindows11_24h2)
		{
			float* rect = (float*)((unsigned char*)*(void**)context + COverlayContext_DeviceClipBox_offset_w11_24h2);
			left = (int)rect[2];
			top = (int)rect[3];
		}
		else if (isWindows11)
		{
			float* rect = (float*)((unsigned char*)*(void**)context + COverlayContext_DeviceClipBox_offset_w11);
			left = (int)rect[0];
			top = (int)rect[1];
		}
		else
		{
			int* rect = (int*)((unsigned char*)context + COverlayContext_DeviceClipBox_offset);
			left = rect[0];
			top = rect[1];
		}
	}
	__except (EXCEPTION_EXECUTE_HANDLER)
	{
		left = 0;
		top = 0;
		LOG_ONLY_ONCE("SEH exception in GetMonitorPositionFromContext — defaulting to (0,0)");
	}
}

lutData* GetLUTDataFromCOverlayContext(void* context, bool hdr)
{
	int left, top;
	GetMonitorPositionFromContext(context, left, top);

	for (int i = 0; i < numLuts; i++)
	{
		if (luts[i].left == left && luts[i].top == top && luts[i].isHdr == hdr)
		{
			return &luts[i];
		}
	}

	// 25H2 workaround: HDR primary context may need opposite hdr flag LUT
	if (isWindows11_25h2 && g_primaryHdrContext == context)
	{
		for (int i = 0; i < numLuts; i++)
		{
			if (luts[i].left == left && luts[i].top == top && luts[i].isHdr != hdr)
			{
				return &luts[i];
			}
		}
	}

	// No LUT staged for this monitor position — skip LUT application
	return NULL;
}

void InitializeStuff(ID3D11Device* inputDevice)
{
	try
	{
		device = inputDevice;
		device->AddRef();
		LOG_ONLY_ONCE("Device successfully gathered")
		LOG_ADDRESS("The device address is: ", device)

		device->GetImmediateContext(&deviceContext);
		LOG_ONLY_ONCE("Got context after device")
		LOG_ADDRESS("The Device context is located at address: ", deviceContext)

		{
			ID3DBlob* vsBlob;
			ID3DBlob* compile_error_interface;
			LOG_ONLY_ONCE(("Trying to compile vshader with this code:\n" + std::string(shaders)).c_str())
			EXECUTE_D3DCOMPILE_WITH_LOG(
				D3DCompile(shaders, sizeof shaders, NULL, NULL, NULL, "VS", "vs_5_0", 0, 0, &vsBlob, &
					compile_error_interface), compile_error_interface)


			LOG_ONLY_ONCE("Vertex shader compiled successfully")
			EXECUTE_WITH_LOG(device->CreateVertexShader(vsBlob->GetBufferPointer(),
				vsBlob->GetBufferSize(), NULL, &vertexShader))


			LOG_ONLY_ONCE("Vertex shader created successfully")
			D3D11_INPUT_ELEMENT_DESC inputElementDesc[] =
			{
				{"POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0},
				{
					"TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, D3D11_APPEND_ALIGNED_ELEMENT,
					D3D11_INPUT_PER_VERTEX_DATA, 0
				}
			};
			EXECUTE_WITH_LOG(device->CreateInputLayout(inputElementDesc, ARRAYSIZE(inputElementDesc),
				vsBlob->GetBufferPointer(),
				vsBlob->GetBufferSize(), &inputLayout))

			vsBlob->Release();
		}
		{
			ID3DBlob* psBlob;
			ID3DBlob* compile_error_interface;
			EXECUTE_D3DCOMPILE_WITH_LOG(
				D3DCompile(shaders, sizeof shaders, NULL, NULL, NULL, "PS", "ps_5_0", 0, 0, &psBlob, &
					compile_error_interface), compile_error_interface)

			LOG_ONLY_ONCE("Pixel shader compiled successfully")
			device->CreatePixelShader(psBlob->GetBufferPointer(),
			                          psBlob->GetBufferSize(), NULL, &pixelShader);
			psBlob->Release();
		}
		{
			stride = 4 * sizeof(float);
			numVerts = 4;
			offset = 0;

			D3D11_BUFFER_DESC vertexBufferDesc = {};
			vertexBufferDesc.ByteWidth = stride * numVerts;
			vertexBufferDesc.Usage = D3D11_USAGE_DYNAMIC;
			vertexBufferDesc.BindFlags = D3D11_BIND_VERTEX_BUFFER;
			vertexBufferDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;

			EXECUTE_WITH_LOG(device->CreateBuffer(&vertexBufferDesc, NULL, &vertexBuffer))
		}
		{
			D3D11_SAMPLER_DESC samplerDesc = {};
			samplerDesc.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
			samplerDesc.AddressU = samplerDesc.AddressV = samplerDesc.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
			samplerDesc.ComparisonFunc = D3D11_COMPARISON_NEVER;

			EXECUTE_WITH_LOG(device->CreateSamplerState(&samplerDesc, &samplerState))
		}
		for (int i = 0; i < numLuts; i++)
		{
			lutData* lut = &luts[i];

			D3D11_TEXTURE3D_DESC desc = {};
			desc.Width = lut->size;
			desc.Height = lut->size;
			desc.Depth = lut->size;
			desc.MipLevels = 1;
			desc.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
			desc.Usage = D3D11_USAGE_IMMUTABLE;
			desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

			D3D11_SUBRESOURCE_DATA initData;
			initData.pSysMem = lut->rawLut;
			initData.SysMemPitch = lut->size * 4 * sizeof(float);
			initData.SysMemSlicePitch = lut->size * lut->size * 4 * sizeof(float);

			ID3D11Texture3D* tex;
			EXECUTE_WITH_LOG(device->CreateTexture3D(&desc, &initData, &tex))
			EXECUTE_WITH_LOG(device->CreateShaderResourceView((ID3D11Resource*)tex, NULL, &luts[i].textureView))
			tex->Release();
			free(lut->rawLut);
			lut->rawLut = NULL;
		}
		{
			D3D11_SAMPLER_DESC samplerDesc = {};
			samplerDesc.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
			samplerDesc.AddressU = samplerDesc.AddressV = samplerDesc.AddressW = D3D11_TEXTURE_ADDRESS_WRAP;
			samplerDesc.ComparisonFunc = D3D11_COMPARISON_NEVER;

			EXECUTE_WITH_LOG(device->CreateSamplerState(&samplerDesc, &noiseSamplerState))
		}
		{
			D3D11_TEXTURE2D_DESC desc = {};
			desc.Width = NOISE_SIZE;
			desc.Height = NOISE_SIZE;
			desc.MipLevels = 1;
			desc.ArraySize = 1;
			desc.Format = DXGI_FORMAT_R32_FLOAT;
			desc.SampleDesc.Count = 1;
			desc.SampleDesc.Quality = 0;
			desc.Usage = D3D11_USAGE_IMMUTABLE;
			desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

			float noise[NOISE_SIZE][NOISE_SIZE];

			for (int i = 0; i < NOISE_SIZE; i++)
			{
				for (int j = 0; j < NOISE_SIZE; j++)
				{
					noise[i][j] = (noiseBytes[i][j] + 0.5) / 256;
				}
			}

			D3D11_SUBRESOURCE_DATA initData;
			initData.pSysMem = noise;
			initData.SysMemPitch = sizeof(noise[0]);

			ID3D11Texture2D* tex;
			EXECUTE_WITH_LOG(device->CreateTexture2D(&desc, &initData, &tex))
			EXECUTE_WITH_LOG(device->CreateShaderResourceView((ID3D11Resource*)tex, NULL, &noiseTextureView))
			tex->Release();
		}
		{
			D3D11_BUFFER_DESC constantBufferDesc = {};
			constantBufferDesc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
			constantBufferDesc.ByteWidth = 16;
			constantBufferDesc.Usage = D3D11_USAGE_DYNAMIC;
			constantBufferDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;

			EXECUTE_WITH_LOG(device->CreateBuffer(&constantBufferDesc, NULL, &constantBuffer))
			LOG_ONLY_ONCE("Final buffer created in InitializeStuff")
		}
	}
	catch (std::exception& ex)
	{
		std::stringstream ex_message;
		ex_message << "Exception caught at line " << __LINE__ << ": " << ex.what() << std::endl;
		log_to_file(ex_message.str().c_str());
		RELEASE_IF_NOT_NULL(device)
		RELEASE_IF_NOT_NULL(deviceContext)
		device = nullptr;
		deviceContext = nullptr;
		return;
	}
	catch (...)
	{
		std::stringstream ex_message;
		ex_message << "Exception caught at line " << __LINE__ << ": (unknown)" << std::endl;
		log_to_file(ex_message.str().c_str());
		RELEASE_IF_NOT_NULL(device)
		RELEASE_IF_NOT_NULL(deviceContext)
		device = nullptr;
		deviceContext = nullptr;
		return;
	}
}

void UninitializeStuff()
{
	RELEASE_IF_NOT_NULL(device)
	RELEASE_IF_NOT_NULL(deviceContext)
	RELEASE_IF_NOT_NULL(vertexShader)
	RELEASE_IF_NOT_NULL(pixelShader)
	RELEASE_IF_NOT_NULL(inputLayout)
	RELEASE_IF_NOT_NULL(vertexBuffer)
	RELEASE_IF_NOT_NULL(samplerState)
	for (int i = 0; i < 2; i++)
	{
		RELEASE_IF_NOT_NULL(texture[i])
		RELEASE_IF_NOT_NULL(textureView[i])
	}
	RELEASE_IF_NOT_NULL(noiseSamplerState)
	RELEASE_IF_NOT_NULL(noiseTextureView)
	RELEASE_IF_NOT_NULL(constantBuffer)
	for (int i = 0; i < numLuts; i++)
	{
		free(luts[i].rawLut);
		RELEASE_IF_NOT_NULL(luts[i].textureView)
	}
	free(luts);
	free(lutTargets);
}


bool RenderLUT(void* cOverlayContext, ID3D11Texture2D* backBuffer, struct tagRECT* rects, int numRects)
{
	ID3D11RenderTargetView* renderTargetView;

	D3D11_TEXTURE2D_DESC newBackBufferDesc;
	backBuffer->GetDesc(&newBackBufferDesc);

	int index = -1;
	int colorMode = 0;  // 0=SDR 8-bit, 1=HDR, 2=ACM SDR
	if (newBackBufferDesc.Format == DXGI_FORMAT_B8G8R8A8_UNORM ||
	    newBackBufferDesc.Format == DXGI_FORMAT_R8G8B8A8_UNORM ||
	    newBackBufferDesc.Format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB ||
	    newBackBufferDesc.Format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB ||
	    newBackBufferDesc.Format == DXGI_FORMAT_R10G10B10A2_UNORM)
	{
		index = 0;
		colorMode = 0;  // Legacy SDR
	}
	else if (newBackBufferDesc.Format == DXGI_FORMAT_R16G16B16A16_FLOAT)
	{
		// FP16 could be HDR or ACM SDR — check DXGI output state
		int monLeft = 0, monTop = 0;
		GetMonitorPositionFromContext(cOverlayContext, monLeft, monTop);

		bool monitorIsHdr = IsMonitorHdr(monLeft, monTop);
		if (monitorIsHdr) {
			index = 1;
			colorMode = 1;  // HDR
		} else {
			index = 1;       // ACM SDR is also FP16 — must use FP16 staging texture (not index=0 which is B8G8R8A8)
			colorMode = 2;   // ACM SDR (FP16 linear, SDR LUT)
		}

		if (isWindows11_25h2 && g_primaryHdrContext == NULL && monitorIsHdr)
		{
			g_primaryHdrContext = cOverlayContext;
		}
	}

	// Log per-context info for diagnostics (track up to 8 unique contexts)
	{
		static void* loggedContexts[8] = {};
		static int numLoggedContexts = 0;
		bool alreadyLogged = false;
		for (int lc = 0; lc < numLoggedContexts; lc++) {
			if (loggedContexts[lc] == cOverlayContext) { alreadyLogged = true; break; }
		}
		if (!alreadyLogged) {
			int dbgLeft = 0, dbgTop = 0;
			GetMonitorPositionFromContext(cOverlayContext, dbgLeft, dbgTop);
			char msg[256];
			sprintf(msg, "RenderLUT: ctx=%p pos=(%d,%d) fmt=%d size=%ux%u colorMode=%d",
				cOverlayContext, dbgLeft, dbgTop, (int)newBackBufferDesc.Format,
				newBackBufferDesc.Width, newBackBufferDesc.Height, colorMode);
			log_to_file(msg);
			if (numLoggedContexts < 8) loggedContexts[numLoggedContexts++] = cOverlayContext;
		}
	}

	// For ACM SDR, look for SDR LUT (isHdr=false); for HDR, look for HDR LUT
	bool lookForHdrLut = (colorMode == 1);
	lutData* lut;
	if (index == -1 || !(lut = GetLUTDataFromCOverlayContext(cOverlayContext, lookForHdrLut)))
	{
		return false;  // No LUT for this monitor — skip silently
	}

	D3D11_TEXTURE2D_DESC oldTextureDesc = textureDesc[index];
	if (newBackBufferDesc.Width > oldTextureDesc.Width || newBackBufferDesc.Height > oldTextureDesc.Height)
	{
		if (texture[index] != NULL)
		{
			texture[index]->Release();
			textureView[index]->Release();
		}

		UINT newWidth = max(newBackBufferDesc.Width, oldTextureDesc.Width);
		UINT newHeight = max(newBackBufferDesc.Height, oldTextureDesc.Height);

		D3D11_TEXTURE2D_DESC newTextureDesc;

		newTextureDesc = newBackBufferDesc;
		newTextureDesc.Width = newWidth;
		newTextureDesc.Height = newHeight;
		newTextureDesc.Usage = D3D11_USAGE_DEFAULT;
		newTextureDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
		newTextureDesc.CPUAccessFlags = 0;
		newTextureDesc.MiscFlags = 0;

		textureDesc[index] = newTextureDesc;

		EXECUTE_WITH_LOG(device->CreateTexture2D(&textureDesc[index], NULL, &texture[index]))
		EXECUTE_WITH_LOG(
			device->CreateShaderResourceView((ID3D11Resource*)texture[index], NULL, &textureView[index]))
	}

	backBufferDesc = newBackBufferDesc;

	EXECUTE_WITH_LOG(device->CreateRenderTargetView((ID3D11Resource*)backBuffer, NULL, &renderTargetView))
	const D3D11_VIEWPORT d3d11_viewport(0, 0, backBufferDesc.Width, backBufferDesc.Height, 0.0f, 1.0f);
	deviceContext->RSSetViewports(1, &d3d11_viewport);

	deviceContext->OMSetRenderTargets(1, &renderTargetView, NULL);
	renderTargetView->Release();

	deviceContext->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
	deviceContext->IASetInputLayout(inputLayout);

	deviceContext->VSSetShader(vertexShader, NULL, 0);
	deviceContext->PSSetShader(pixelShader, NULL, 0);

	deviceContext->PSSetShaderResources(0, 1, &textureView[index]);
	deviceContext->PSSetShaderResources(1, 1, &lut->textureView);
	deviceContext->PSSetSamplers(0, 1, &samplerState);

	deviceContext->PSSetShaderResources(2, 1, &noiseTextureView);
	deviceContext->PSSetSamplers(1, 1, &noiseSamplerState);

	// Dither levels: 255 for 8-bit, 1023 for R10G10B10A2, 255 default
	int dLevels = 255;
	if (newBackBufferDesc.Format == DXGI_FORMAT_R10G10B10A2_UNORM)
		dLevels = 1023;
	int constantData[4] = {lut->size, colorMode, dLevels, 0};

	D3D11_MAPPED_SUBRESOURCE resource;
	EXECUTE_WITH_LOG(deviceContext->Map((ID3D11Resource*)constantBuffer, 0, D3D11_MAP_WRITE_DISCARD, 0,
		&resource))
	memcpy(resource.pData, constantData, sizeof(constantData));
	deviceContext->Unmap((ID3D11Resource*)constantBuffer, 0);

	deviceContext->PSSetConstantBuffers(0, 1, &constantBuffer);

	for (int i = 0; i < numRects; i++)
	{
		D3D11_BOX sourceRegion;
		sourceRegion.left = rects[i].left;
		sourceRegion.right = rects[i].right;
		sourceRegion.top = rects[i].top;
		sourceRegion.bottom = rects[i].bottom;
		sourceRegion.front = 0;
		sourceRegion.back = 1;

		deviceContext->CopySubresourceRegion((ID3D11Resource*)texture[index], 0, rects[i].left,
		                                     rects[i].top, 0, (ID3D11Resource*)backBuffer, 0, &sourceRegion);
		DrawRectangle(&rects[i], index);
	}

	return true;
}

bool ApplyLUT(void* cOverlayContext, IDXGISwapChain* swapChain, struct tagRECT* rects, int numRects)
{
	try
	{
		// Cache monitor position for this context via swapchain output (25H2: context has no position data)
		{
			static void* cachedContexts[16] = {};
			static int numCached = 0;
			bool alreadyCached = false;
			for (int c = 0; c < numCached; c++) {
				if (cachedContexts[c] == cOverlayContext) { alreadyCached = true; break; }
			}
			if (!alreadyCached) {
				IDXGIOutput* output = NULL;
				if (SUCCEEDED(swapChain->GetContainingOutput(&output))) {
					DXGI_OUTPUT_DESC desc;
					if (SUCCEEDED(output->GetDesc(&desc))) {
						CacheContextPosition(cOverlayContext, desc.DesktopCoordinates.left, desc.DesktopCoordinates.top);
						char msg[256];
						sprintf(msg, "Cached context %p position from swapchain: (%ld,%ld) %ldx%ld",
							cOverlayContext, desc.DesktopCoordinates.left, desc.DesktopCoordinates.top,
							desc.DesktopCoordinates.right - desc.DesktopCoordinates.left,
							desc.DesktopCoordinates.bottom - desc.DesktopCoordinates.top);
						log_to_file(msg);
					}
					output->Release();
				} else {
					char msg[128];
					sprintf(msg, "GetContainingOutput failed for context %p", cOverlayContext);
					log_to_file(msg);
				}
				if (numCached < 16) cachedContexts[numCached++] = cOverlayContext;
			}
		}

		if (!device)
		{
			LOG_ONLY_ONCE("Initializing stuff in ApplyLUT")
			ID3D11Device* dev;
			EXECUTE_WITH_LOG(swapChain->GetDevice(IID_ID3D11Device, (void**)&dev))
			InitializeStuff(dev);
			dev->Release();
		}
		LOG_ONLY_ONCE("Init done, continuing with LUT application")

		ID3D11Texture2D* backBuffer;
		EXECUTE_WITH_LOG(swapChain->GetBuffer(0, IID_ID3D11Texture2D, (void**)&backBuffer))

		bool result = RenderLUT(cOverlayContext, backBuffer, rects, numRects);
		backBuffer->Release();
		return result;
	}
	catch (std::exception& ex)
	{
		std::stringstream ex_message;
		ex_message << "Exception caught at line " << __LINE__ << ": " << ex.what() << std::endl;
		log_to_file(ex_message.str().c_str());
		return false;
	}
	catch (...)
	{
		std::stringstream ex_message;
		ex_message << "Exception caught at line " << __LINE__ << std::endl;
		log_to_file(ex_message.str().c_str());
		return false;
	}
}


bool ApplyLUTDirect(void* cOverlayContext, ID3D11Texture2D* backBuffer, struct tagRECT* rects, int numRects)
{
	try
	{
		if (!device)
		{
			LOG_ONLY_ONCE("Initializing from texture device (25H2)")
			ID3D11Device* dev;
			backBuffer->GetDevice(&dev);
			InitializeStuff(dev);
			dev->Release();
		}

		// 25H2: Cache context position using monitor data from monitors.dat
		{
			static void* cachedContexts[16] = {};
			static int numCached = 0;
			bool alreadyCached = false;
			for (int c = 0; c < numCached; c++) {
				if (cachedContexts[c] == cOverlayContext) { alreadyCached = true; break; }
			}
			if (!alreadyCached) {
				D3D11_TEXTURE2D_DESC bbDesc;
				backBuffer->GetDesc(&bbDesc);
				bool isFP16 = (bbDesc.Format == DXGI_FORMAT_R16G16B16A16_FLOAT);

				// Find matching monitors from g_monitorHdrStates (already populated in InitializeStuff)
				int matchIndices[8];
				int numMatches = 0;
				for (int m = 0; m < g_numMonitorHdrStates && numMatches < 8; m++) {
					if (g_monitorHdrStates[m].width == bbDesc.Width &&
					    g_monitorHdrStates[m].height == bbDesc.Height) {
						matchIndices[numMatches++] = m;
					}
				}

				if (numMatches == 1) {
					auto& ms = g_monitorHdrStates[matchIndices[0]];
					CacheContextPosition(cOverlayContext, ms.left, ms.top);
					char msg[256];
					sprintf(msg, "25H2: Cached ctx %p pos (%d,%d) unique match (%ux%u fmt=%d)",
						cOverlayContext, ms.left, ms.top, bbDesc.Width, bbDesc.Height, (int)bbDesc.Format);
					log_to_file(msg);
				} else if (numMatches > 1) {
					int bestIdx = -1;

					// Check if BPC values differ
					bool bpcVaries = false;
					for (int i = 1; i < numMatches; i++) {
						if (g_monitorHdrStates[matchIndices[i]].bpc != g_monitorHdrStates[matchIndices[0]].bpc) {
							bpcVaries = true; break;
						}
					}

					if (bpcVaries) {
						bestIdx = 0;
						for (int i = 1; i < numMatches; i++) {
							if (isFP16) {
								if (g_monitorHdrStates[matchIndices[i]].bpc > g_monitorHdrStates[matchIndices[bestIdx]].bpc)
									bestIdx = i;
							} else {
								if (g_monitorHdrStates[matchIndices[i]].bpc < g_monitorHdrStates[matchIndices[bestIdx]].bpc)
									bestIdx = i;
							}
						}
					} else {
						// Same BPC — assign by order, skipping already-cached positions
						for (int i = 0; i < numMatches; i++) {
							auto& ms = g_monitorHdrStates[matchIndices[i]];
							bool alreadyUsed = false;
							for (int c = 0; c < g_numContextPosCache; c++) {
								if (g_contextPosCache[c].left == ms.left && g_contextPosCache[c].top == ms.top) {
									alreadyUsed = true; break;
								}
							}
							if (!alreadyUsed) { bestIdx = i; break; }
						}
						if (bestIdx < 0) bestIdx = 0;
					}

					auto& ms = g_monitorHdrStates[matchIndices[bestIdx]];
					CacheContextPosition(cOverlayContext, ms.left, ms.top);
					char msg[256];
					sprintf(msg, "25H2: Cached ctx %p pos (%d,%d) %s (bpc=%u, fp16=%d, %d candidates)",
						cOverlayContext, ms.left, ms.top,
						bpcVaries ? "bpc-match" : "order-match",
						ms.bpc, isFP16 ? 1 : 0, numMatches);
					log_to_file(msg);
				} else {
					char msg[128];
					sprintf(msg, "25H2: No output match for ctx %p (%ux%u fmt=%d)",
						cOverlayContext, bbDesc.Width, bbDesc.Height, (int)bbDesc.Format);
					log_to_file(msg);
				}
				if (numCached < 16) cachedContexts[numCached++] = cOverlayContext;
			}
		}

		return RenderLUT(cOverlayContext, backBuffer, rects, numRects);
	}
	catch (std::exception& ex)
	{
		std::stringstream ex_message;
		ex_message << "Exception caught at line " << __LINE__ << ": " << ex.what() << std::endl;
		log_to_file(ex_message.str().c_str());
		return false;
	}
	catch (...)
	{
		std::stringstream ex_message;
		ex_message << "Exception caught at line " << __LINE__ << std::endl;
		log_to_file(ex_message.str().c_str());
		return false;
	}
}

typedef struct rectVec
{
	struct tagRECT* start;
	struct tagRECT* end;
	struct tagRECT* cap;
} rectVec;

typedef long (COverlayContext_Present_t)(void*, void*, unsigned int, rectVec*, unsigned int, bool);
typedef long long (COverlayContext_Present_24h2_t)(void*, void*, unsigned int, rectVec*, int, void*, bool);



static ID3D11Texture2D* GetBackBuffer_25H2(void* overlaySwapChain)
{
	__try
	{
		if (!overlaySwapChain) return NULL;

		void** vt = *(void***)overlaySwapChain;
		if (!vt) return NULL;

		typedef void* (__fastcall *VirtFunc)(void*);

		VirtFunc func1 = (VirtFunc)vt[24];
		if (!func1) return NULL;

		void* r1 = func1(overlaySwapChain);
		if (!r1) return NULL;

		void** vt2 = *(void***)r1;
		if (!vt2) return NULL;

		VirtFunc func2 = (VirtFunc)vt2[19];
		if (!func2) return NULL;

		void* r2 = func2(r1);
		if (!r2) return NULL;

		ID3D11Texture2D* tex = NULL;
		HRESULT hr = ((IUnknown*)r2)->QueryInterface(IID_ID3D11Texture2D, (void**)&tex);
		if (FAILED(hr) || !tex) return NULL;

		LOG_ONLY_ONCE("25H2: Got texture via overlaySwapChain->vt[24]()->vt2[19]()->QI")
		return tex;
	}
	__except (EXCEPTION_EXECUTE_HANDLER)
	{
		return NULL;
	}
}

COverlayContext_Present_t* COverlayContext_Present_orig = NULL;
COverlayContext_Present_t* COverlayContext_Present_real_orig = NULL;

COverlayContext_Present_24h2_t* COverlayContext_Present_orig_24h2 = NULL;
COverlayContext_Present_24h2_t* COverlayContext_Present_real_orig_24h2 = NULL;

long long COverlayContext_Present_hook_24h2(void* self, void* overlaySwapChain, unsigned int a3, rectVec* rectVec,
	int a5, void* a6, bool a7)
{
	if (_ReturnAddress() < (void*)COverlayContext_Present_real_orig_24h2 || isWindows11_24h2 || isWindows11_25h2)
	{
			LOG_ONLY_ONCE("I am inside COverlayContext::Present hook inside the main if condition")
			std::stringstream overlay_swapchain_message;
			overlay_swapchain_message << "OverlaySwapChain address: 0x" << std::hex << overlaySwapChain
				<< " -- windows 11 25h2: " << isWindows11_25h2
				<< " -- windows 11 24h2: " << isWindows11_24h2
				<< " -- " << "windows 11: " << isWindows11;
			LOG_ONLY_ONCE(overlay_swapchain_message.str().c_str())

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
			if (isWindows11_24h2)
				hwProtected = *((bool*)overlaySwapChain + IOverlaySwapChain_HardwareProtected_offset_w11_24h2);
			else if (isWindows11)
				hwProtected = *((bool*)overlaySwapChain + IOverlaySwapChain_HardwareProtected_offset_w11);
			else
				hwProtected = *((bool*)overlaySwapChain + IOverlaySwapChain_HardwareProtected_offset);

			if (hwProtected)
			{
				LOG_ONLY_ONCE("Hardware protected - unsetting LUT active")
				UnsetLUTActive(self);
			}
			else
			{
				IDXGISwapChain* swapChain = NULL;

				if (isWindows11_24h2)
				{
					LOG_ONLY_ONCE("Gathering IDXGISwapChain pointer")
					swapChain = *(IDXGISwapChain**)((unsigned char*)overlaySwapChain +
						IOverlaySwapChain_IDXGISwapChain_offset_w11_24h2);
				}
				else if (isWindows11)
				{
					LOG_ONLY_ONCE("Gathering IDXGISwapChain pointer")
					int sub_from_legacy_swapchain = *(int*)((unsigned char*)overlaySwapChain - 4);
					void* real_overlay_swap_chain = (unsigned char*)overlaySwapChain - sub_from_legacy_swapchain -
						0x1b0;
					swapChain = *(IDXGISwapChain**)((unsigned char*)real_overlay_swap_chain +
						IOverlaySwapChain_IDXGISwapChain_offset_w11);
				}
				else
				{
					swapChain = *(IDXGISwapChain**)((unsigned char*)overlaySwapChain +
						IOverlaySwapChain_IDXGISwapChain_offset);
				}

				if (swapChain != NULL && ApplyLUT(self, swapChain, rectVec->start, rectVec->end - rectVec->start))
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
	}

	return COverlayContext_Present_orig_24h2(self, overlaySwapChain, a3, rectVec, a5, a6, a7);
}


long COverlayContext_Present_hook(void* self, void* overlaySwapChain, unsigned int a3, rectVec* rectVec,
                                  unsigned int a5, bool a6)
{
	if (_ReturnAddress() < (void*)COverlayContext_Present_real_orig)
	{
		LOG_ONLY_ONCE("I am inside COverlayContext::Present hook inside the main if condition")

		bool hwProtected = false;
		if (isWindows11)
			hwProtected = *((bool*)overlaySwapChain + IOverlaySwapChain_HardwareProtected_offset_w11);
		else
			hwProtected = *((bool*)overlaySwapChain + IOverlaySwapChain_HardwareProtected_offset);

		if (hwProtected)
		{
			LOG_ONLY_ONCE("Hardware protected - unsetting LUT active")
			UnsetLUTActive(self);
		}
		else
		{
			IDXGISwapChain* swapChain;

			if (isWindows11)
			{
				LOG_ONLY_ONCE("Gathering IDXGISwapChain pointer")
				int sub_from_legacy_swapchain = *(int*)((unsigned char*)overlaySwapChain - 4);
				void* real_overlay_swap_chain = (unsigned char*)overlaySwapChain - sub_from_legacy_swapchain -
					0x1b0;
				swapChain = *(IDXGISwapChain**)((unsigned char*)real_overlay_swap_chain +
					IOverlaySwapChain_IDXGISwapChain_offset_w11);
			}
			else
			{
				swapChain = *(IDXGISwapChain**)((unsigned char*)overlaySwapChain +
					IOverlaySwapChain_IDXGISwapChain_offset);
			}

			if (ApplyLUT(self, swapChain, rectVec->start, rectVec->end - rectVec->start))
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

	return COverlayContext_Present_orig(self, overlaySwapChain, a3, rectVec, a5, a6);
}

typedef bool (CWindowContext_IsCandidateDirectFlipCompatbile_t)(void*, void*, bool);
CWindowContext_IsCandidateDirectFlipCompatbile_t* CWindowContext_IsCandidateDirectFlipCompatbile_orig = NULL;

bool CWindowContext_IsCandidateDirectFlipCompatbile_hook(void* self, void* a2, bool a3)
{
	if (numLuts > 0)
	{
		return false;
	}
	return CWindowContext_IsCandidateDirectFlipCompatbile_orig(self, a2, a3);
}

typedef bool (CCompSwapChain_IsCandidateDirectFlipCompatbile_t)(void*, void*, bool);
CCompSwapChain_IsCandidateDirectFlipCompatbile_t* CCompSwapChain_IsCandidateDirectFlipCompatbile_orig = NULL;

bool CCompSwapChain_IsCandidateDirectFlipCompatbile_hook(void* self, void* a2, bool a3)
{
	if (numLuts > 0)
	{
		return false;
	}
	return CCompSwapChain_IsCandidateDirectFlipCompatbile_orig(self, a2, a3);
}

typedef bool (CCompVisual_IsCandidateForPromotion_t)(void*, void*, void*);
CCompVisual_IsCandidateForPromotion_t* CCompVisual_IsCandidateForPromotion_orig = NULL;

bool CCompVisual_IsCandidateForPromotion_hook(void* self, void* a2, void* a3)
{
	if (numLuts > 0)
	{
		return false;
	}
	return CCompVisual_IsCandidateForPromotion_orig(self, a2, a3);
}

typedef bool (CCompSwapChain_IsCandidateIndependentFlipCompatible_t)(void*);
CCompSwapChain_IsCandidateIndependentFlipCompatible_t* CCompSwapChain_IsCandidateIndependentFlipCompatible_orig = NULL;

bool CCompSwapChain_IsCandidateIndependentFlipCompatible_hook(void* self)
{
	if (numLuts > 0)
	{
		return false;
	}
	return CCompSwapChain_IsCandidateIndependentFlipCompatible_orig(self);
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
			log_to_file("DLL_PROCESS_ATTACH: DllMain entered");
			HMODULE dwmcore = GetModuleHandle(L"dwmcore.dll");
			MODULEINFO moduleInfo;
			GetModuleInformation(GetCurrentProcess(), dwmcore, &moduleInfo, sizeof moduleInfo);

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
								sprintf(msg, "monitors.dat: (%d,%d) %s bpc=%u %ux%u",
									left, top, ms.isHdr ? "HDR" : "SDR", ms.bpc, ms.width, ms.height);
								log_to_file(msg);

								g_numMonitorHdrStates++;
							}
						}
					}
					fclose(mf);
					char msg[64];
					sprintf(msg, "Loaded %d monitors from monitors.dat", g_numMonitorHdrStates);
					log_to_file(msg);
				} else {
					log_to_file("WARNING: monitors.dat not found, position matching disabled");
				}
				g_hdrStatesDetected = true;
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
						for (int j = 0; j < 500; j++) {
							unsigned char* fAddr = address + j;
							if (!memcmp(fAddr, flipMatch, 3)) {
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
					if (!COverlayContext_Present_orig && sizeof COverlayContext_Present_bytes_w11_24h2 <= moduleInfo.
						SizeOfImage - i && !aob_match_inverse(address, COverlayContext_Present_bytes_w11_24h2,
							sizeof COverlayContext_Present_bytes_w11_24h2))
					{
						COverlayContext_Present_orig_24h2 = (COverlayContext_Present_24h2_t*)address;
						COverlayContext_Present_real_orig_24h2 = COverlayContext_Present_orig_24h2;
					}
					else if (!COverlayContext_IsCandidateDirectFlipCompatbile_orig && sizeof
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
					if (COverlayContext_Present_orig && COverlayContext_IsCandidateDirectFlipCompatbile_orig &&
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
			if (numLuts > 0 && ((COverlayContext_Present_orig && COverlayContext_IsCandidateDirectFlipCompatbile_orig &&
				COverlayContext_OverlaysEnabled_orig) ||
				(COverlayContext_Present_orig_24h2 && COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2 && COverlayContext_OverlaysEnabled_orig)))

			{
				MH_Initialize();
				if (!isWindows11_24h2 && !isWindows11_25h2)
					MH_CreateHook((PVOID)COverlayContext_Present_orig, (PVOID)COverlayContext_Present_hook,
								  (PVOID*)&COverlayContext_Present_orig);
				else
					MH_CreateHook((PVOID)COverlayContext_Present_orig_24h2, (PVOID)COverlayContext_Present_hook_24h2,
						(PVOID*)&COverlayContext_Present_orig_24h2);

				if (!isWindows11_24h2 && !isWindows11_25h2)
					MH_CreateHook((PVOID)COverlayContext_IsCandidateDirectFlipCompatbile_orig,
								  (PVOID)COverlayContext_IsCandidateDirectFlipCompatbile_hook,
								  (PVOID*)&COverlayContext_IsCandidateDirectFlipCompatbile_orig);
				else
					MH_CreateHook((PVOID)COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2,
						(PVOID)COverlayContext_IsCandidateDirectFlipCompatbile_hook_24h2,
						(PVOID*)&COverlayContext_IsCandidateDirectFlipCompatbile_orig_24h2);

				if (CWindowContext_IsCandidateDirectFlipCompatbile_orig)
				{
					MH_CreateHook((PVOID)CWindowContext_IsCandidateDirectFlipCompatbile_orig,
						(PVOID)CWindowContext_IsCandidateDirectFlipCompatbile_hook,
						(PVOID*)&CWindowContext_IsCandidateDirectFlipCompatbile_orig);
					LOG_ONLY_ONCE("Hooked CWindowContext::IsCandidateDirectFlipCompatible")
				}
				else {
					LOG_ONLY_ONCE("FAILED to find CWindowContext::IsCandidateDirectFlipCompatible")
				}

				if (CCompSwapChain_IsCandidateIndependentFlipCompatible_orig)
				{
					MH_CreateHook((PVOID)CCompSwapChain_IsCandidateIndependentFlipCompatible_orig,
						(PVOID)CCompSwapChain_IsCandidateIndependentFlipCompatible_hook,
						(PVOID*)&CCompSwapChain_IsCandidateIndependentFlipCompatible_orig);
					LOG_ONLY_ONCE("Hooked CCompSwapChain::IsCandidateIndependentFlipCompatible")
				}
				else {
					LOG_ONLY_ONCE("FAILED to find CCompSwapChain::IsCandidateIndependentFlipCompatible")
				}

				if (CCompSwapChain_IsCandidateDirectFlipCompatbile_orig)
				{
					MH_CreateHook((PVOID)CCompSwapChain_IsCandidateDirectFlipCompatbile_orig,
						(PVOID)CCompSwapChain_IsCandidateDirectFlipCompatbile_hook,
						(PVOID*)&CCompSwapChain_IsCandidateDirectFlipCompatbile_orig);
					LOG_ONLY_ONCE("Hooked CCompSwapChain::IsCandidateDirectFlipCompatible")
				}
				else {
					LOG_ONLY_ONCE("FAILED to find CCompSwapChain::IsCandidateDirectFlipCompatible")
				}

				if (CCompVisual_IsCandidateForPromotion_orig)
				{
					MH_CreateHook((PVOID)CCompVisual_IsCandidateForPromotion_orig,
						(PVOID)CCompVisual_IsCandidateForPromotion_hook,
						(PVOID*)&CCompVisual_IsCandidateForPromotion_orig);
					LOG_ONLY_ONCE("Hooked CCompVisual::IsCandidateForPromotion")
				}
				else {
					LOG_ONLY_ONCE("FAILED to find CCompVisual::IsCandidateForPromotion")
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
				MH_EnableHook(MH_ALL_HOOKS);
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
			// Short timeout to avoid deadlock under loader lock
			if (WaitForSingleObject(g_hostMonitorThread, 5000) == WAIT_TIMEOUT) {
				log_to_file("WARNING: Host monitor thread did not exit in 5s");
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
			MH_Uninitialize();
			Sleep(100);
		}
		UninitializeStuff();
		log_to_file("DLL_PROCESS_DETACH: cleanup complete");
		break;
	default:
		break;
	}
	return TRUE;
}
