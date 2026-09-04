#include "pch.h"
#include "hook_render.h"
#include "hook_log.h"
#include "hook_shader.h"
#include "hook_lut.h"
#include "noise.h"
#include "dwm_hook_config.h"

#include <string>
#include <sstream>
#include <iomanip>
#include <cmath>
#include <cstdint>
#include <cstring>

// Defined in dllmain.cpp
extern bool isWindows11;
extern bool isWindows11_24h2;
extern bool isWindows11_25h2;
extern const int COverlayContext_DeviceClipBox_offset;
extern int COverlayContext_DeviceClipBox_offset_w11;
extern int COverlayContext_DeviceClipBox_offset_w11_24h2;

// D3D11 resource globals
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

// PQ transfer function LUTs (for ICtCp tonemapping)
ID3D11Texture2D* pqOetfTexture = NULL;
ID3D11ShaderResourceView* pqOetfSRV = NULL;
ID3D11Texture2D* pqEotfTexture = NULL;
ID3D11ShaderResourceView* pqEotfSRV = NULL;
ID3D11SamplerState* linearSamplerState = NULL;

// Peak detection compute shader resources
ID3D11ComputeShader* peakDetectCS = NULL;
ID3D11Texture2D* peakTexture = NULL;
ID3D11UnorderedAccessView* peakUAV = NULL;
ID3D11ShaderResourceView* peakSRV = NULL;
ID3D11Buffer* peakCB = NULL;

// Per-monitor HDR state detected via DXGI output enumeration
MonitorHdrState g_monitorHdrStates[16];
int g_numMonitorHdrStates = 0;
bool g_hdrStatesDetected = false;

bool IsMonitorHdr(int left, int top) {
	for (int i = 0; i < g_numMonitorHdrStates; i++) {
		if (g_monitorHdrStates[i].left == left && g_monitorHdrStates[i].top == top)
			return g_monitorHdrStates[i].isHdr;
	}
	return false;  // default to SDR if unknown (safer — avoids PQ pipeline on SDR content)
}

// Cache: map cOverlayContext pointers to monitor positions (populated via swapchain in ApplyLUT)
ContextPositionCache g_contextPosCache[16] = {};
int g_numContextPosCache = 0;

void CacheContextPositionEx(void* context, int left, int top, int method) {
	for (int i = 0; i < g_numContextPosCache; i++) {
		if (g_contextPosCache[i].context == context) {
			g_contextPosCache[i].left = left;
			g_contextPosCache[i].top = top;
			if (method != CTXPOS_UNKNOWN) g_contextPosCache[i].method = method;
			return;
		}
	}
	if (g_numContextPosCache < 16) {
		g_contextPosCache[g_numContextPosCache++] = { context, left, top, method };
	}
}

void CacheContextPosition(void* context, int left, int top) {
	CacheContextPositionEx(context, left, top, CTXPOS_UNKNOWN);
}

// ---------------------------------------------------------------------------
// Twin-panel routing persistence (25H2) — see DWM_HOOK_ROUTING_FILE_A in dwm_hook_config.h.
// Two identical panels are only told apart by first-present order, re-rolled on every
// injection. The DLL records what it assigned; the next injection of the SAME dwm.exe reuses
// it (pins), and the host can rewrite the file (swap) after verifying through a meter.
// Everything here is plain C I/O on a tiny file — same cost class as log_to_file, and only
// touched when a context is seen for the first time after injection.
// ---------------------------------------------------------------------------
RoutingPin g_routingPins[16] = {};
int g_numRoutingPins = 0;
bool g_routingPinsValid = false;
bool g_routingConfirmed = false;

const char* CtxPosMethodName(int method) {
	switch (method) {
	case CTXPOS_UNIQUE: return "unique";
	case CTXPOS_BPC:    return "bpc";
	case CTXPOS_SCAN:   return "scan";
	case CTXPOS_PINNED: return "pinned";
	case CTXPOS_ORDER:  return "order";
	case CTXPOS_LEGACY: return "legacy";
	default:            return "unknown";
	}
}

static const char* RoutingFilePath() {
	static char path[MAX_PATH] = {0};
	if (path[0] == '\0') {
		char pattern[MAX_PATH] = {0};
		snprintf(pattern, sizeof(pattern), DWM_HOOK_ROUTING_FILE_FMT_A, (unsigned long)GetCurrentProcessId());
		ExpandEnvironmentStringsA(pattern, path, sizeof(path));
	}
	return path;
}

// Set when THIS injection had to order-match a context (a fresh roll); only then may the
// DLL lower the host's meter-verified `confirmed` flag (F3: any other first-encounter —
// a fullscreen swapchain, a monitor wake — must preserve what the host wrote).
static bool g_routingRolled = false;

static int ReadConfirmedFromFile() {
	FILE* f = fopen(RoutingFilePath(), "r");
	if (!f) return 0;
	int confirmed = 0;
	char line[256];
	while (fgets(line, sizeof(line), f)) {
		int v = 0;
		if (sscanf(line, "confirmed %d", &v) == 1) { confirmed = v; break; }
	}
	fclose(f);
	return confirmed;
}

// This dwm.exe's identity: pid + creation time. Context pointers are only meaningful inside
// one DWM process lifetime, so pins from a previous DWM are ignored.
static void DwmSessionKey(DWORD& pid, DWORD& high, DWORD& low) {
	pid = GetCurrentProcessId();
	FILETIME c = {}, e = {}, k = {}, u = {};
	if (GetProcessTimes(GetCurrentProcess(), &c, &e, &k, &u)) {
		high = c.dwHighDateTime;
		low = c.dwLowDateTime;
	} else {
		high = low = 0;
	}
}

void LoadRoutingPins() {
	g_numRoutingPins = 0;
	g_routingPinsValid = false;
	g_routingConfirmed = false;
	g_routingRolled = false;
	FILE* f = fopen(RoutingFilePath(), "r");
	if (!f) {
		log_to_file("routing: no persisted state (first injection for this dwm.exe, or cleared)");
		return;
	}
	DWORD myPid = 0, myHigh = 0, myLow = 0;
	DwmSessionKey(myPid, myHigh, myLow);
	bool sessionOk = false;
	int monLines = 0, monMatched = 0, confirmed = 0, version = 0;
	char line[256];
	while (fgets(line, sizeof(line), f)) {
		unsigned long pid = 0, high = 0, low = 0;
		int l = 0, t = 0, w = 0, h = 0, bpc = 0;
		unsigned long long ptr = 0;
		char method[16] = {0};
		if (sscanf(line, DWM_HOOK_ROUTING_MAGIC " %d", &version) == 1) continue;
		if (sscanf(line, "session %lu %lu %lu", &pid, &high, &low) == 3) {
			sessionOk = (pid == myPid && high == myHigh && low == myLow);
			continue;
		}
		if (sscanf(line, "mon %d %d %d %d %d", &l, &t, &w, &h, &bpc) == 5) {
			monLines++;
			for (int m = 0; m < g_numMonitorHdrStates; m++) {
				const MonitorHdrState& ms = g_monitorHdrStates[m];
				if (ms.left == l && ms.top == t && (int)ms.width == w && (int)ms.height == h && (int)ms.bpc == bpc) {
					monMatched++;
					break;
				}
			}
			continue;
		}
		if (sscanf(line, "confirmed %d", &confirmed) == 1) continue;
		if (sscanf(line, "ctx %llx %d %d %15s", &ptr, &l, &t, method) >= 3) {
			if (g_numRoutingPins < 16)
				g_routingPins[g_numRoutingPins++] = { (void*)(uintptr_t)ptr, l, t };
			continue;
		}
	}
	fclose(f);
	bool topologyOk = (monLines == g_numMonitorHdrStates) && (monMatched == monLines);
	g_routingPinsValid = sessionOk && topologyOk && g_numRoutingPins > 0;
	g_routingConfirmed = g_routingPinsValid && confirmed != 0;
	char msg[256];
	snprintf(msg, sizeof(msg), "routing: persisted state %s (session %s, topology %s, %d pins, confirmed=%d)",
		g_routingPinsValid ? "HONOURED" : "ignored", sessionOk ? "match" : "MISMATCH",
		topologyOk ? "match" : "MISMATCH", g_numRoutingPins, confirmed);
	log_to_file(msg);
	if (!g_routingPinsValid) g_numRoutingPins = 0;
}

void SaveRoutingState() {
	FILE* f = fopen(RoutingFilePath(), "w");
	if (!f) {
		log_to_file("routing: WARNING cannot write the routing state file");
		return;
	}
	DWORD pid = 0, high = 0, low = 0;
	DwmSessionKey(pid, high, low);
	fprintf(f, "%s 1\n", DWM_HOOK_ROUTING_MAGIC);
	fprintf(f, "session %lu %lu %lu\n", (unsigned long)pid, (unsigned long)high, (unsigned long)low);
	for (int m = 0; m < g_numMonitorHdrStates; m++) {
		const MonitorHdrState& ms = g_monitorHdrStates[m];
		fprintf(f, "mon %d %d %u %u %u\n", ms.left, ms.top, ms.width, ms.height, ms.bpc);
	}
	fprintf(f, "confirmed %d\n", g_routingConfirmed ? 1 : 0);
	int written = 0;
	for (int c = 0; c < g_numContextPosCache && written < 16; c++, written++) {
		const ContextPositionCache& e = g_contextPosCache[c];
		fprintf(f, "ctx %llx %d %d %s\n", (unsigned long long)(uintptr_t)e.context, e.left, e.top,
			CtxPosMethodName(e.method));
	}
	// Pins for contexts that have not presented yet this injection stay on disk (F2): a
	// re-injection can come seconds after this one (every set_3dlut), before the second twin
	// draws a frame — dropping its pin would re-roll it.
	if (g_routingPinsValid) {
		for (int p = 0; p < g_numRoutingPins && written < 16; p++) {
			bool seen = false;
			for (int c = 0; c < g_numContextPosCache; c++)
				if (g_contextPosCache[c].context == g_routingPins[p].context) { seen = true; break; }
			if (seen) continue;
			fprintf(f, "ctx %llx %d %d %s\n", (unsigned long long)(uintptr_t)g_routingPins[p].context,
				g_routingPins[p].left, g_routingPins[p].top, CtxPosMethodName(CTXPOS_PINNED));
			written++;
		}
	}
	fclose(f);
}

// The file is the host's channel too: it writes `confirmed 1` after a meter check while we are
// injected. Re-read it before every rewrite unless this injection rolled (F3).
static void RefreshConfirmedBeforeSave() {
	if (g_routingRolled) { g_routingConfirmed = false; return; }
	g_routingConfirmed = ReadConfirmedFromFile() != 0;
}

// NOTE (2026-09-04, HW-probed on 25H2 build 26200): a guarded value scan of each overlay context
// (ctx+0..0xA000 and *(void**)ctx+0..0x9000) for the candidate monitors' desktop rects found ONLY
// device-space rects (0,0,3840,2160) in BOTH twin contexts and never the second panel's
// (-3840,485,...) rect; ctx+0x7698 (the documented 25H2 DeviceClipBox) read (0,1,0,3840)/(0,1,0,0).
// There is no monitor identity to read from the context object on 25H2 — hence the persisted
// pins above + the host's meter-verified swap, instead of another struct offset.

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
void GetMonitorPositionFromContext(void* context, int& left, int& top)
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

void UninitializeStuff();  // Forward declaration for error cleanup

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
			{
				char shaderMsg[64];
				snprintf(shaderMsg, sizeof(shaderMsg), "Compiling shaders (%zu bytes)", sizeof g_shaders);
				LOG_ONLY_ONCE(shaderMsg)
			}
			EXECUTE_D3DCOMPILE_WITH_LOG(
				D3DCompile(g_shaders, sizeof g_shaders, NULL, NULL, NULL, "VS", "vs_5_0", 0, 0, &vsBlob, &
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
				D3DCompile(g_shaders, sizeof g_shaders, NULL, NULL, NULL, "PS", "ps_5_0", 0, 0, &psBlob, &
					compile_error_interface), compile_error_interface)

			LOG_ONLY_ONCE("Pixel shader compiled successfully")
			EXECUTE_WITH_LOG(device->CreatePixelShader(psBlob->GetBufferPointer(),
			                                          psBlob->GetBufferSize(), NULL, &pixelShader))
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
			constantBufferDesc.ByteWidth = 48;  // Expanded for tonemap params (3 x float4)
			constantBufferDesc.Usage = D3D11_USAGE_DYNAMIC;
			constantBufferDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;

			EXECUTE_WITH_LOG(device->CreateBuffer(&constantBufferDesc, NULL, &constantBuffer))
			LOG_ONLY_ONCE("Constant buffer created (48 bytes)")
		}
		// Create linear sampler for PQ 1D LUT hardware bilinear interpolation
		{
			D3D11_SAMPLER_DESC samplerDesc = {};
			samplerDesc.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
			samplerDesc.AddressU = samplerDesc.AddressV = samplerDesc.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
			samplerDesc.ComparisonFunc = D3D11_COMPARISON_NEVER;
			EXECUTE_WITH_LOG(device->CreateSamplerState(&samplerDesc, &linearSamplerState))
			LOG_ONLY_ONCE("Linear sampler created for PQ LUTs")
		}
		// Generate PQ transfer function LUTs
		{
			const int PQ_LUT_SIZE = 4096;
			const float pq_m1 = 0.1593017578125f;
			const float pq_m2 = 78.84375f;
			const float pq_c1 = 0.8359375f;
			const float pq_c2 = 18.8515625f;
			const float pq_c3 = 18.6875f;

			// PQ OETF: sqrt-domain — entry i = PQ((i/(N-1))^2)
			float oetfData[PQ_LUT_SIZE];
			for (int i = 0; i < PQ_LUT_SIZE; i++) {
				float t = (float)i / (float)(PQ_LUT_SIZE - 1);
				float L = t * t;  // sqrt-domain
				float Y = fmaxf(L, 1e-12f);
				float Ym = powf(Y, pq_m1);
				oetfData[i] = powf((pq_c1 + pq_c2 * Ym) / (1.0f + pq_c3 * Ym), pq_m2);
			}

			// PQ EOTF: uniform — entry i = EOTF(i/(N-1))
			float eotfData[PQ_LUT_SIZE];
			for (int i = 0; i < PQ_LUT_SIZE; i++) {
				float pq = (float)i / (float)(PQ_LUT_SIZE - 1);
				float Vm = powf(fmaxf(pq, 1e-12f), 1.0f / pq_m2);
				float t = fmaxf(Vm - pq_c1, 0.0f) / fmaxf(pq_c2 - pq_c3 * Vm, 1e-12f);
				eotfData[i] = powf(t, 1.0f / pq_m1);
			}

			D3D11_TEXTURE2D_DESC pqDesc = {};
			pqDesc.Width = PQ_LUT_SIZE;
			pqDesc.Height = 1;
			pqDesc.MipLevels = 1;
			pqDesc.ArraySize = 1;
			pqDesc.Format = DXGI_FORMAT_R32_FLOAT;
			pqDesc.SampleDesc.Count = 1;
			pqDesc.Usage = D3D11_USAGE_IMMUTABLE;
			pqDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

			D3D11_SUBRESOURCE_DATA oetfInit = {};
			oetfInit.pSysMem = oetfData;
			oetfInit.SysMemPitch = PQ_LUT_SIZE * sizeof(float);
			EXECUTE_WITH_LOG(device->CreateTexture2D(&pqDesc, &oetfInit, &pqOetfTexture))
			EXECUTE_WITH_LOG(device->CreateShaderResourceView((ID3D11Resource*)pqOetfTexture, NULL, &pqOetfSRV))

			D3D11_SUBRESOURCE_DATA eotfInit = {};
			eotfInit.pSysMem = eotfData;
			eotfInit.SysMemPitch = PQ_LUT_SIZE * sizeof(float);
			EXECUTE_WITH_LOG(device->CreateTexture2D(&pqDesc, &eotfInit, &pqEotfTexture))
			EXECUTE_WITH_LOG(device->CreateShaderResourceView((ID3D11Resource*)pqEotfTexture, NULL, &pqEotfSRV))

			LOG_ONLY_ONCE("PQ transfer LUTs created (4096-entry OETF sqrt-domain + EOTF uniform)")
		}
		// Create peak detection resources (1x1 R32_FLOAT for temporal smoothing)
		{
			D3D11_TEXTURE2D_DESC peakDesc = {};
			peakDesc.Width = 1;
			peakDesc.Height = 1;
			peakDesc.MipLevels = 1;
			peakDesc.ArraySize = 1;
			peakDesc.Format = DXGI_FORMAT_R32_FLOAT;
			peakDesc.SampleDesc.Count = 1;
			peakDesc.Usage = D3D11_USAGE_DEFAULT;
			peakDesc.BindFlags = D3D11_BIND_UNORDERED_ACCESS | D3D11_BIND_SHADER_RESOURCE;
			float zero = 0.0f;
			D3D11_SUBRESOURCE_DATA peakInit = {};
			peakInit.pSysMem = &zero;
			peakInit.SysMemPitch = sizeof(float);
			EXECUTE_WITH_LOG(device->CreateTexture2D(&peakDesc, &peakInit, &peakTexture))
			EXECUTE_WITH_LOG(device->CreateUnorderedAccessView((ID3D11Resource*)peakTexture, NULL, &peakUAV))
			EXECUTE_WITH_LOG(device->CreateShaderResourceView((ID3D11Resource*)peakTexture, NULL, &peakSRV))

			D3D11_BUFFER_DESC peakCbDesc = {};
			peakCbDesc.ByteWidth = 32;  // 8 floats
			peakCbDesc.Usage = D3D11_USAGE_DYNAMIC;
			peakCbDesc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
			peakCbDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
			EXECUTE_WITH_LOG(device->CreateBuffer(&peakCbDesc, NULL, &peakCB))

			LOG_ONLY_ONCE("Peak detection resources created")
		}
		// Compile peak detection compute shader
		{
			ID3DBlob* csBlob = NULL;
			ID3DBlob* csError = NULL;
			HRESULT hr = D3DCompile(g_peakDetectShader, sizeof(g_peakDetectShader), NULL, NULL, NULL,
				"main", "cs_5_0", 0, 0, &csBlob, &csError);
			if (SUCCEEDED(hr) && csBlob) {
				EXECUTE_WITH_LOG(device->CreateComputeShader(csBlob->GetBufferPointer(),
					csBlob->GetBufferSize(), NULL, &peakDetectCS))
				csBlob->Release();
				LOG_ONLY_ONCE("Peak detection compute shader compiled OK")
			} else {
				if (csError) {
					std::stringstream ss;
					ss << "Peak detection CS compile error: " << (char*)csError->GetBufferPointer();
					log_to_file(ss.str().c_str());
					csError->Release();
				}
				log_to_file("WARNING: Peak detection CS compilation failed — dynamic tonemapping disabled");
			}
		}
	}
	catch (std::exception& ex)
	{
		std::stringstream ex_message;
		ex_message << "Exception caught at line " << __LINE__ << ": " << ex.what() << std::endl;
		log_to_file(ex_message.str().c_str());
		UninitializeStuff();
		return;
	}
	catch (...)
	{
		std::stringstream ex_message;
		ex_message << "Exception caught at line " << __LINE__ << ": (unknown)" << std::endl;
		log_to_file(ex_message.str().c_str());
		UninitializeStuff();
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
	RELEASE_IF_NOT_NULL(linearSamplerState)
	RELEASE_IF_NOT_NULL(pqOetfTexture)
	RELEASE_IF_NOT_NULL(pqOetfSRV)
	RELEASE_IF_NOT_NULL(pqEotfTexture)
	RELEASE_IF_NOT_NULL(pqEotfSRV)
	RELEASE_IF_NOT_NULL(peakDetectCS)
	RELEASE_IF_NOT_NULL(peakTexture)
	RELEASE_IF_NOT_NULL(peakUAV)
	RELEASE_IF_NOT_NULL(peakSRV)
	RELEASE_IF_NOT_NULL(peakCB)
	// Snapshot + clear first so a concurrent reader sees an empty list before we free.
	int oldNumLuts = numLuts;
	lutData* oldLuts = luts;
	numLuts = 0;
	luts = NULL;
	numLutTargets = 0;
	for (int i = 0; i < MAX_LUT_TARGETS; i++) lutTargets[i] = NULL;

	for (int i = 0; i < oldNumLuts; i++)
	{
		free(oldLuts[i].rawLut);
		RELEASE_IF_NOT_NULL(oldLuts[i].textureView)
	}
	free(oldLuts);
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
		// FP16 backbuffer is either HDR (scRGB, PQ pipeline) or ACM SDR (scRGB, sRGB
		// pipeline). The monitor's *current* mode is the only correct discriminator:
		// a configured HDR LUT does NOT mean the monitor is in HDR right now — users
		// keep an HDR LUT loaded across HDR/SDR mode switches — so LUT presence must
		// not force the pipeline. The previous "prefer HDR LUT presence" heuristic
		// pinned every ACM-SDR monitor to the HDR pipeline whenever an HDR LUT was
		// configured: it ran PQ math on SDR content and silently ignored the SDR LUT.
		// g_monitorHdrStates is fed by the debounced shared-memory monitor updates, so
		// it is already safe against transient re-brokering / fullscreen-video HDR
		// flips, and IsMonitorHdr defaults to SDR (the safe pipeline) when unknown.
		int monLeft = 0, monTop = 0;
		GetMonitorPositionFromContext(cOverlayContext, monLeft, monTop);

		index = 1;  // FP16 staging texture either way

		// Use the known monitor mode when this position is in the detected state list.
		// Only fall back to LUT-presence evidence when the position is absent (e.g. a
		// bogus/uncached context position) — there monitor state is genuinely unknown.
		bool monKnown = false, monIsHdr = false;
		for (int m = 0; m < g_numMonitorHdrStates; m++) {
			if (g_monitorHdrStates[m].left == monLeft && g_monitorHdrStates[m].top == monTop) {
				monKnown = true;
				monIsHdr = g_monitorHdrStates[m].isHdr;
				break;
			}
		}
		if (monKnown) {
			colorMode = monIsHdr ? 1 : 2;  // 1=HDR, 2=ACM SDR
		} else {
			bool hdrLutAtPos = false, sdrLutAtPos = false;
			for (int k = 0; k < numLuts; k++) {
				if (luts[k].left == monLeft && luts[k].top == monTop) {
					if (luts[k].isHdr) hdrLutAtPos = true;
					else sdrLutAtPos = true;
				}
			}
			// Only an HDR-only hint routes to HDR; both-or-neither defaults to ACM SDR.
			colorMode = (hdrLutAtPos && !sdrLutAtPos) ? 1 : 2;
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
			snprintf(msg, sizeof(msg), "RenderLUT: ctx=%p pos=(%d,%d) fmt=%d size=%ux%u colorMode=%d",
				cOverlayContext, dbgLeft, dbgTop, (int)newBackBufferDesc.Format,
				newBackBufferDesc.Width, newBackBufferDesc.Height, colorMode);
			log_to_file(msg);
			if (numLoggedContexts < 8) loggedContexts[numLoggedContexts++] = cOverlayContext;
		}
	}

	// For ACM SDR, look for SDR LUT (isHdr=false); for HDR, look for HDR LUT
	bool lookForHdrLut = (colorMode == 1);
	lutData* lut = NULL;
	if (index != -1)
		lut = GetLUTDataFromCOverlayContext(cOverlayContext, lookForHdrLut);

	// Look up tonemap params for this monitor
	int monLeft = 0, monTop = 0;
	if (!lut && index != -1) {
		// Need position even without LUT for tonemap lookup
		GetMonitorPositionFromContext(cOverlayContext, monLeft, monTop);
	} else if (lut) {
		monLeft = lut->left;
		monTop = lut->top;
	}
	LocalTonemapParams* tp = FindTonemapForMonitor(monLeft, monTop);
	bool tmEnabled = (colorMode == 1 && tp && tp->enabled);

	// Skip if no LUT AND no tonemap — nothing to render
	if (index == -1 || (!lut && !tmEnabled))
	{
		return false;
	}

	D3D11_TEXTURE2D_DESC oldTextureDesc = textureDesc[index];
	if (newBackBufferDesc.Width > oldTextureDesc.Width || newBackBufferDesc.Height > oldTextureDesc.Height)
	{
		// Release old and null the slots before recreating, so a throw from
		// CreateTexture2D/CreateShaderResourceView leaves slots cleanly empty
		// rather than dangling (next RenderLUT would Release() a freed pointer).
		if (texture[index] != NULL)
		{
			texture[index]->Release();
			texture[index] = NULL;
		}
		if (textureView[index] != NULL)
		{
			textureView[index]->Release();
			textureView[index] = NULL;
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

	bool hasLutOrTonemap = (lut || tmEnabled);

	if (hasLutOrTonemap) {
	  try {
		EXECUTE_WITH_LOG(device->CreateRenderTargetView((ID3D11Resource*)backBuffer, NULL, &renderTargetView))
		const D3D11_VIEWPORT d3d11_viewport(0, 0, backBufferDesc.Width, backBufferDesc.Height, 0.0f, 1.0f);
		deviceContext->RSSetViewports(1, &d3d11_viewport);

		deviceContext->OMSetRenderTargets(1, &renderTargetView, NULL);
		renderTargetView->Release();

		deviceContext->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
		deviceContext->IASetInputLayout(inputLayout);

		deviceContext->VSSetShader(vertexShader, NULL, 0);
		deviceContext->PSSetShader(pixelShader, NULL, 0);

		// Bind shader resources
		deviceContext->PSSetShaderResources(0, 1, &textureView[index]);
		if (lut)
			deviceContext->PSSetShaderResources(1, 1, &lut->textureView);
		deviceContext->PSSetSamplers(0, 1, &samplerState);

		deviceContext->PSSetShaderResources(2, 1, &noiseTextureView);
		deviceContext->PSSetSamplers(1, 1, &noiseSamplerState);

		// PQ LUTs + linear sampler for tonemapping
		if (pqOetfSRV) deviceContext->PSSetShaderResources(3, 1, &pqOetfSRV);
		if (pqEotfSRV) deviceContext->PSSetShaderResources(4, 1, &pqEotfSRV);
		if (linearSamplerState) deviceContext->PSSetSamplers(2, 1, &linearSamplerState);

		// Peak detection compute pass (before pixel shader)
		if (tmEnabled && tp->dynamicPeak && peakDetectCS && peakUAV && peakCB) {
			// Update peak CB
			struct { UINT fw, fh; float rise, fall, maxRise, maxFall; float pad[2]; } peakParams;
			peakParams.fw = newBackBufferDesc.Width;
			peakParams.fh = newBackBufferDesc.Height;
			peakParams.rise = 0.3f;   // Rise rate
			peakParams.fall = 0.05f;  // Fall rate
			peakParams.maxRise = 100.0f;
			peakParams.maxFall = 50.0f;
			peakParams.pad[0] = peakParams.pad[1] = 0.0f;

			D3D11_MAPPED_SUBRESOURCE peakRes;
			if (SUCCEEDED(deviceContext->Map((ID3D11Resource*)peakCB, 0, D3D11_MAP_WRITE_DISCARD, 0, &peakRes))) {
				memcpy(peakRes.pData, &peakParams, sizeof(peakParams));
				deviceContext->Unmap((ID3D11Resource*)peakCB, 0);
			}

			deviceContext->CSSetShader(peakDetectCS, NULL, 0);
			deviceContext->CSSetConstantBuffers(0, 1, &peakCB);
			deviceContext->CSSetShaderResources(0, 1, &textureView[index]);  // Input: captured backbuffer
			deviceContext->CSSetUnorderedAccessViews(0, 1, &peakUAV, NULL);
			deviceContext->Dispatch(1, 1, 1);

			// Unbind CS UAV and bind peak SRV for pixel shader
			ID3D11UnorderedAccessView* nullUAV = NULL;
			deviceContext->CSSetUnorderedAccessViews(0, 1, &nullUAV, NULL);
			deviceContext->CSSetShader(NULL, NULL, 0);
		}
		// Bind peak texture for pixel shader read (even if not dynamic — shader checks tonemapDynamic)
		if (peakSRV) deviceContext->PSSetShaderResources(5, 1, &peakSRV);

		// Fill expanded constant buffer (48 bytes = 12 ints/floats)
		int dLevels = 255;
		if (newBackBufferDesc.Format == DXGI_FORMAT_R10G10B10A2_UNORM)
			dLevels = 1023;

		struct {
			int lutSize;
			int colorMode;
			int ditherLevels;
			int tonemapEnabled;
			int tonemapCurve;
			float tonemapTargetNits;
			float pqSourcePeak;
			float pqTargetPeak;
			int tonemapDynamic;
			int hasLut;
			float pad1;
			float pad2;
		} cb;
		static_assert(sizeof(cb) == 48, "Constant buffer size must match ByteWidth=48 in InitializeStuff");
		cb.lutSize = lut ? lut->size : 0;
		cb.colorMode = colorMode;
		cb.ditherLevels = dLevels;
		cb.tonemapEnabled = tmEnabled ? 1 : 0;
		cb.tonemapCurve = tmEnabled ? (int)tp->curve : 0;
		cb.tonemapTargetNits = tmEnabled ? tp->targetPeakNits : 1000.0f;
		cb.pqTargetPeak = tmEnabled ? tp->pqTargetPeak : 0.0f;
		cb.tonemapDynamic = (tmEnabled && tp->dynamicPeak) ? 1 : 0;
		// Dynamic mode: floor = target peak (or raised floor for BT.2390/BT.2446A)
		// Static mode: PQ of user-specified source peak
		if (tmEnabled && tp->dynamicPeak) {
			float floorNits = tp->targetPeakNits;
			if (tp->curve == DWMHOOK_TONEMAP_BT2390 || tp->curve == DWMHOOK_TONEMAP_BT2446A) {
				float tgtClamped = tp->targetPeakNits > 400.0f ? tp->targetPeakNits : 400.0f;
				float t = (tgtClamped - 400.0f) / 3600.0f;
				if (t > 1.0f) t = 1.0f;
				floorNits = tp->targetPeakNits * (1.0f + t * 0.5f);
			}
			cb.pqSourcePeak = LinearToPQ(floorNits / 10000.0f);
		} else {
			cb.pqSourcePeak = tmEnabled ? tp->pqSourcePeak : 0.0f;
		}
		cb.hasLut = lut ? 1 : 0;
		cb.pad1 = 0.0f;
		cb.pad2 = 0.0f;

		// Diagnostic: log tonemap CB state only when values change (avoids per-frame spam)
		if (tmEnabled) {
			static int prevCurve = -1;
			static float prevPqSrc = -1.0f, prevPqTgt = -1.0f;
			if (cb.tonemapCurve != prevCurve || cb.pqSourcePeak != prevPqSrc || cb.pqTargetPeak != prevPqTgt) {
				prevCurve = cb.tonemapCurve;
				prevPqSrc = cb.pqSourcePeak;
				prevPqTgt = cb.pqTargetPeak;
				char diagMsg[256];
				snprintf(diagMsg, sizeof(diagMsg), "TM DIAG: tmEn=%d dyn=%d curve=%d pqSrc=%.4f pqTgt=%.4f tgtNits=%.0f srcNits=%.0f csOK=%d uavOK=%d",
					cb.tonemapEnabled, cb.tonemapDynamic, cb.tonemapCurve,
					cb.pqSourcePeak, cb.pqTargetPeak, cb.tonemapTargetNits,
					tp->sourcePeakNits, peakDetectCS ? 1 : 0, peakUAV ? 1 : 0);
				log_to_file(diagMsg);
			}
		}

		D3D11_MAPPED_SUBRESOURCE resource;
		EXECUTE_WITH_LOG(deviceContext->Map((ID3D11Resource*)constantBuffer, 0, D3D11_MAP_WRITE_DISCARD, 0,
			&resource))
		memcpy(resource.pData, &cb, sizeof(cb));
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
	  }
	  catch (...) {
		// Cleanup on exception — prevent stale bindings on DWM's device context
		ID3D11ShaderResourceView* nullSRVs[6] = {};
		deviceContext->PSSetShaderResources(0, 6, nullSRVs);
		ID3D11SamplerState* nullSamplers[3] = {};
		deviceContext->PSSetSamplers(0, 3, nullSamplers);
		ID3D11Buffer* nullCB = NULL;
		deviceContext->PSSetConstantBuffers(0, 1, &nullCB);
		deviceContext->CSSetConstantBuffers(0, 1, &nullCB);
		deviceContext->VSSetShader(NULL, NULL, 0);
		deviceContext->PSSetShader(NULL, NULL, 0);
		deviceContext->OMSetRenderTargets(0, NULL, NULL);
		throw;
	  }
	}

	// Clean up ALL state we set — DWM reuses this device context for its own rendering.
	// Any stale bindings can collide with DWM's own shaders and cause GPU faults.
	// The original hook left t0-t2, s0-s1, b0 (16-byte) bound and DWM tolerated it,
	// but our expanded 48-byte b0 has non-zero data at offsets 16+ that DWM's shader
	// may misinterpret if it reads from its own (larger) constant buffer at b0.
	{
		ID3D11ShaderResourceView* nullSRVs[6] = {};
		deviceContext->PSSetShaderResources(0, 6, nullSRVs);  // Clear t0-t5
		ID3D11SamplerState* nullSamplers[3] = {};
		deviceContext->PSSetSamplers(0, 3, nullSamplers);      // Clear s0-s2
		ID3D11Buffer* nullCB = NULL;
		deviceContext->PSSetConstantBuffers(0, 1, &nullCB);    // Clear b0
		deviceContext->CSSetConstantBuffers(0, 1, &nullCB);    // Clear CS b0
		deviceContext->VSSetShader(NULL, NULL, 0);
		deviceContext->PSSetShader(NULL, NULL, 0);
		deviceContext->OMSetRenderTargets(0, NULL, NULL);
	}

	return hasLutOrTonemap;  // Only report active when backbuffer was actually modified
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
						CacheContextPositionEx(cOverlayContext, desc.DesktopCoordinates.left, desc.DesktopCoordinates.top, CTXPOS_LEGACY);
						char msg[256];
						snprintf(msg, sizeof(msg), "Cached context %p position from swapchain: (%ld,%ld) %ldx%ld",
							cOverlayContext, desc.DesktopCoordinates.left, desc.DesktopCoordinates.top,
							desc.DesktopCoordinates.right - desc.DesktopCoordinates.left,
							desc.DesktopCoordinates.bottom - desc.DesktopCoordinates.top);
						log_to_file(msg);
						RefreshConfirmedBeforeSave();
						SaveRoutingState();
					}
					output->Release();
				} else {
					char msg[128];
					snprintf(msg, sizeof(msg), "GetContainingOutput failed for context %p", cOverlayContext);
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
					CacheContextPositionEx(cOverlayContext, ms.left, ms.top, CTXPOS_UNIQUE);
					char msg[256];
					snprintf(msg, sizeof(msg), "25H2: Cached ctx %p pos (%d,%d) unique match (%ux%u fmt=%d)",
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
					}

					int method = bpcVaries ? CTXPOS_BPC : CTXPOS_ORDER;
					if (!bpcVaries) {
						// Same size AND same BPC: 25H2 gives no identity, so first-present ORDER decides —
						// a coin toss re-rolled on every injection (2026-09-03: the ProArt's cube rendered on
						// the LG C6 for a whole calibration run). Honour the assignment this dwm.exe made
						// on its previous injection (possibly swapped by the host after a meter check);
						// only a context never seen before falls back to order.
						if (g_routingPinsValid) {
							for (int p = 0; p < g_numRoutingPins; p++) {
								if (g_routingPins[p].context != cOverlayContext) continue;
								for (int i = 0; i < numMatches; i++) {
									auto& pm = g_monitorHdrStates[matchIndices[i]];
									if (pm.left == g_routingPins[p].left && pm.top == g_routingPins[p].top) {
										bestIdx = i;
										method = CTXPOS_PINNED;
										break;
									}
								}
								break;
							}
						}
						if (bestIdx < 0) {
							// Assign by order. Pass 1 skips positions already cached AND positions a pin
							// reserves for a context that has not presented yet; pass 2 (F1: a stale pin
							// for a context DWM recreated at a new address) skips only cached positions —
							// a live context beats a reservation for one that may never come; then 0.
							for (int pass = 0; pass < 2 && bestIdx < 0; pass++) {
								for (int i = 0; i < numMatches; i++) {
									auto& om = g_monitorHdrStates[matchIndices[i]];
									bool alreadyUsed = false;
									for (int c = 0; c < g_numContextPosCache; c++) {
										if (g_contextPosCache[c].left == om.left && g_contextPosCache[c].top == om.top) {
											alreadyUsed = true; break;
										}
									}
									if (!alreadyUsed && pass == 0 && g_routingPinsValid) {
										for (int p = 0; p < g_numRoutingPins; p++) {
											if (g_routingPins[p].context != cOverlayContext &&
											    g_routingPins[p].left == om.left && g_routingPins[p].top == om.top) {
												alreadyUsed = true; break;
											}
										}
									}
									if (!alreadyUsed) { bestIdx = i; break; }
								}
							}
							if (bestIdx < 0) bestIdx = 0;
							// A fresh roll: whatever a client verified through the meter no longer holds.
							g_routingRolled = true;
							g_routingConfirmed = false;
						}
					}

					auto& ms = g_monitorHdrStates[matchIndices[bestIdx]];
					CacheContextPositionEx(cOverlayContext, ms.left, ms.top, method);
					char msg[256];
					snprintf(msg, sizeof(msg), "25H2: Cached ctx %p pos (%d,%d) %s (bpc=%u, fp16=%d, %d candidates)",
						cOverlayContext, ms.left, ms.top,
						method == CTXPOS_BPC ? "bpc-match" : (method == CTXPOS_PINNED ? "pinned" : "order-match"),
						ms.bpc, isFP16 ? 1 : 0, numMatches);
					log_to_file(msg);
				} else {
					char msg[128];
					snprintf(msg, sizeof(msg), "25H2: No output match for ctx %p (%ux%u fmt=%d)",
						cOverlayContext, bbDesc.Width, bbDesc.Height, (int)bbDesc.Format);
					log_to_file(msg);
				}
				if (numCached < 16) cachedContexts[numCached++] = cOverlayContext;
				// Persist the assignment for the next injection of this dwm.exe (and for the host's
				// state.get / hook.set_routing). Also written for unique/bpc so the host can tell an
				// unambiguous rig from one it has no evidence about.
				if (numMatches >= 1) { RefreshConfirmedBeforeSave(); SaveRoutingState(); }
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

ID3D11Texture2D* GetBackBuffer_25H2(void* overlaySwapChain)
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
