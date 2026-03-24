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

void CacheContextPosition(void* context, int left, int top) {
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
			LOG_ONLY_ONCE(("Trying to compile vshader with this code:\n" + std::string(g_shaders)).c_str())
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

	bool hasLutOrTonemap = (lut || tmEnabled);

	if (hasLutOrTonemap) {
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

		// Diagnostic: periodic logging of tonemap CB state (no GPU readback — DWM can't tolerate stalls)
		if (tmEnabled) {
			static int tonemapDiagCounter = 0;
			if (++tonemapDiagCounter >= 120) {
				tonemapDiagCounter = 0;
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
						CacheContextPosition(cOverlayContext, desc.DesktopCoordinates.left, desc.DesktopCoordinates.top);
						char msg[256];
						snprintf(msg, sizeof(msg), "Cached context %p position from swapchain: (%ld,%ld) %ldx%ld",
							cOverlayContext, desc.DesktopCoordinates.left, desc.DesktopCoordinates.top,
							desc.DesktopCoordinates.right - desc.DesktopCoordinates.left,
							desc.DesktopCoordinates.bottom - desc.DesktopCoordinates.top);
						log_to_file(msg);
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
					CacheContextPosition(cOverlayContext, ms.left, ms.top);
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
					snprintf(msg, sizeof(msg), "25H2: Cached ctx %p pos (%d,%d) %s (bpc=%u, fp16=%d, %d candidates)",
						cOverlayContext, ms.left, ms.top,
						bpcVaries ? "bpc-match" : "order-match",
						ms.bpc, isFP16 ? 1 : 0, numMatches);
					log_to_file(msg);
				} else {
					char msg[128];
					snprintf(msg, sizeof(msg), "25H2: No output match for ctx %p (%ux%u fmt=%d)",
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
