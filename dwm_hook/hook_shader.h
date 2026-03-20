// DesktopLUT DWM Hook - hook_shader.h
// Shader string literals for the DWM hook: main vertex/pixel shader and peak detection compute shader.
// Extracted from dllmain.cpp for maintainability. These are compiled at runtime via D3DCompile.

#pragma once

#include "hook_log.h"  // STRINGIFY, DITHER_GAMMA
#include "noise.h"     // NOISE_SIZE

// Main vertex + pixel shader (VS, PS) — handles HDR/SDR/ACM color modes, ICtCp tonemapping, 3D LUT, dithering
static char g_shaders[] = R"(
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

Texture2D<float> pqOetfLUT : register(t3);
Texture2D<float> pqEotfLUT : register(t4);
Texture2D<float> peakTexture : register(t5);
SamplerState linearSmp : register(s2);

cbuffer Constants : register(b0) {
	int lutSize;
	int colorMode;       // 0=SDR 8-bit, 1=HDR, 2=ACM SDR
	int ditherLevels;
	int tonemapEnabled;

	int tonemapCurve;    // 0-4 (BT2390, SoftClip, Reinhard, BT2446A, HardClip)
	float tonemapTargetNits;
	float pqSourcePeak;  // Precomputed PQ of source peak (or floor for dynamic)
	float pqTargetPeak;  // Precomputed PQ of target peak

	int tonemapDynamic;
	int hasLut;          // 1 if 3D LUT loaded for this monitor
	float pad1;
	float pad2;
};

// BT.709 <-> Rec.2020 conversion
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

// ICtCp color space matrices (Dolby)
static const float3x3 Rec2020_to_LMS = {
	0.41210938, 0.52392578, 0.06396484,
	0.16674805, 0.72045898, 0.11279297,
	0.02416992, 0.07543945, 0.90039063
};
static const float3x3 LMS_to_Rec2020 = {
	3.43661000, -2.50645000,  0.06984000,
	-0.79133000,  1.98360000, -0.19227000,
	-0.02595000, -0.09891000,  1.12486000
};
static const float3x3 LMSprime_to_ICtCp = {
	0.50000000,  0.50000000,  0.00000000,
	1.61376953, -3.32348633,  1.70971680,
	4.37817383, -4.24560547, -0.13256836
};
static const float3x3 ICtCp_to_LMSprime = {
	1.0,  0.00860904,  0.11102963,
	1.0, -0.00860904, -0.11102963,
	1.0,  0.56003134, -0.32062717
};

// PQ constants for analytical fallback (compute shader peak detection)
static float m1 = 1305 / 8192.;
static float m2 = 2523 / 32.;
static float c1 = 107 / 128.;
static float c2 = 2413 / 128.;
static float c3 = 2392 / 128.;

// PQ via 1D LUT (sqrt-domain OETF, uniform EOTF)
float3 Linear_to_PQ(float3 L) {
	float3 sq = sqrt(max(L, 0.0));
	float scale = 4095.0 / 4096.0;
	float bias = 0.5 / 4096.0;
	return float3(
		pqOetfLUT.SampleLevel(linearSmp, float2(sq.x * scale + bias, 0.5), 0),
		pqOetfLUT.SampleLevel(linearSmp, float2(sq.y * scale + bias, 0.5), 0),
		pqOetfLUT.SampleLevel(linearSmp, float2(sq.z * scale + bias, 0.5), 0));
}
float3 PQ_to_Linear(float3 pq) {
	float scale = 4095.0 / 4096.0;
	float bias = 0.5 / 4096.0;
	return float3(
		pqEotfLUT.SampleLevel(linearSmp, float2(max(pq.x, 0.0) * scale + bias, 0.5), 0),
		pqEotfLUT.SampleLevel(linearSmp, float2(max(pq.y, 0.0) * scale + bias, 0.5), 0),
		pqEotfLUT.SampleLevel(linearSmp, float2(max(pq.z, 0.0) * scale + bias, 0.5), 0));
}
float Linear_to_PQ_scalar(float L) {
	float sq = sqrt(max(L, 0.0));
	return pqOetfLUT.SampleLevel(linearSmp, float2(sq * (4095.0/4096.0) + (0.5/4096.0), 0.5), 0);
}
float PQ_to_Linear_scalar(float pq) {
	return pqEotfLUT.SampleLevel(linearSmp, float2(max(pq, 0.0) * (4095.0/4096.0) + (0.5/4096.0), 0.5), 0);
}

// Analytical PQ (for legacy LUT path when tonemap disabled)
float3 pq_eotf(float3 e) {
	return pow(max((pow(e, 1 / m2) - c1), 0) / (c2 - c3 * pow(e, 1 / m2)), 1 / m1);
}
float3 pq_inv_eotf(float3 y) {
	return pow((c1 + c2 * pow(y, m1)) / (1 + c3 * pow(y, m1)), m2);
}

// ========== Tonemapping curves (PQ-native, from shader.h) ==========

float TonemapBT2390_PQ(float I, float pqSrcPeak, float pqTgtPeak) {
	float iw = pqSrcPeak;
	float ow = pqTgtPeak;
	float E = I / iw;
	float maxLum = ow / iw;
	float KS = 1.5 * maxLum - 0.5;
	KS = max(KS, 0.0);
	if (KS >= 1.0) return clamp(E * iw, 0.0, ow);
	if (E <= KS) return E * iw;
	float t = (E - KS) / (1.0 - KS);
	float t2 = t * t, t3 = t2 * t;
	float h00 = 2.0*t3 - 3.0*t2 + 1.0;
	float h10 = t3 - 2.0*t2 + t;
	float h01 = -2.0*t3 + 3.0*t2;
	float E_mapped = h00 * KS + h10 * (1.0 - KS) + h01 * maxLum;
	return clamp(E_mapped * iw, 0.0, ow);
}

float TonemapSoftClip_PQ(float I, float pqSrcPeak, float pqTgtPeak, float targetNits) {
	float pqKnee = (targetNits <= 203.0) ? 0.0 : pqTgtPeak * 0.8;
	if (I <= pqKnee) return I;
	float overshoot = I - pqKnee;
	float headroom = pqTgtPeak - pqKnee;
	float srcRange = pqSrcPeak - pqKnee;
	return pqKnee + headroom * (1.0 - exp(-overshoot / srcRange));
}

float TonemapReinhard_PQ(float I, float pqSrcPeak, float pqTgtPeak, float targetNits) {
	float pqKnee = (targetNits <= 203.0) ? 0.0 : pqTgtPeak * 0.8;
	if (I <= pqKnee) return I;
	float overshoot = I - pqKnee;
	float headroom = pqTgtPeak - pqKnee;
	float srcRange = pqSrcPeak - pqKnee;
	return pqKnee + headroom * overshoot / (overshoot + srcRange);
}

float TonemapHardClip_PQ(float I, float pqTgtPeak) {
	return min(I, pqTgtPeak);
}

float TonemapBT2446A(float Y, float targetPeak, float targetNits) {
	float knee = (targetNits <= 203.0) ? 0.0 : targetPeak * 0.8;
	if (Y <= knee) return Y;
	float overshoot = Y - knee;
	float maxOvershoot = 1.0 - knee;
	float headroom = targetPeak - knee;
	float normalizedOvershoot = overshoot / maxOvershoot;
	float Yg = pow(normalizedOvershoot, 1.0 / 2.4);
	float compressionRatio = maxOvershoot / headroom;
	float pHDR = 1.0 + 32.0 * pow(compressionRatio, 1.0 / 2.4);
	float pSDR = 1.0 + 32.0;
	float Yp = log(1.0 + (pHDR - 1.0) * Yg) / log(pHDR);
	float Yc;
	if (Yp <= 0.7399)      Yc = Yp * 1.0770;
	else if (Yp < 0.9909)  Yc = Yp * (-1.1510 * Yp + 2.7811) - 0.6302;
	else                    Yc = Yp * 0.5000 + 0.5000;
	float Ysdr = (pow(pSDR, Yc) - 1.0) / (pSDR - 1.0);
	float compressed = pow(max(Ysdr, 0.0), 2.4);
	return knee + compressed * headroom;
}

float3 ApplyTonemappingICtCp(float3 ictcp) {
	float I = ictcp.x;
	if (I <= 0.0) return ictcp;

	float pqSrcPeak;
	if (tonemapDynamic > 0 && tonemapTargetNits > 203.0) {
		float pqDetected = peakTexture.Load(int3(0, 0, 0));
		pqSrcPeak = max(pqDetected, pqSourcePeak);
	} else {
		pqSrcPeak = pqSourcePeak;
	}

	float pqTgtPeak = pqTargetPeak;
	float headroom = pqSrcPeak - pqTgtPeak;
	float margin = pqTgtPeak * 0.03;

	if (headroom <= 0.0) {
		ictcp.x = min(ictcp.x, pqTgtPeak);
		return ictcp;
	}

	float I_mapped;
	if (tonemapCurve == 0)
		I_mapped = TonemapBT2390_PQ(I, pqSrcPeak, pqTgtPeak);
	else if (tonemapCurve == 1)
		I_mapped = TonemapSoftClip_PQ(I, pqSrcPeak, pqTgtPeak, tonemapTargetNits);
	else if (tonemapCurve == 2)
		I_mapped = TonemapReinhard_PQ(I, pqSrcPeak, pqTgtPeak, tonemapTargetNits);
	else if (tonemapCurve == 3) {
		float sourcePeakNits = max(PQ_to_Linear_scalar(pqSrcPeak) * 10000.0, 1.0);
		float nits = PQ_to_Linear_scalar(I) * 10000.0;
		float normalized = nits / sourcePeakNits;
		float targetNormalized = tonemapTargetNits / sourcePeakNits;
		float mapped = TonemapBT2446A(normalized, targetNormalized, tonemapTargetNits);
		I_mapped = Linear_to_PQ_scalar(mapped * sourcePeakNits / 10000.0);
	}
	else
		I_mapped = TonemapHardClip_PQ(I, pqTgtPeak);

	if (headroom < margin) {
		float blend = headroom / margin;
		I_mapped = lerp(min(I, pqTgtPeak), I_mapped, blend);
	}

	return float3(I_mapped, ictcp.y, ictcp.z);
}

float3 ApplyDitherICtCp(float3 ictcp, float2 pos) {
	float2 noiseUV = pos / )" STRINGIFY(NOISE_SIZE) R"(;
	float noiseI  = noiseTex.Sample(noiseSmp, noiseUV).x;
	float noiseCT = noiseTex.Sample(noiseSmp, noiseUV + float2(0.5, 0.0)).x;
	float noiseCP = noiseTex.Sample(noiseSmp, noiseUV + float2(0.0, 0.5)).x;
	float ditherI  = (noiseI  - 0.5) / 1023.0;
	float ditherCT = (noiseCT - 0.5) / 2046.0;
	float ditherCP = (noiseCP - 0.5) / 2046.0;
	return ictcp + float3(ditherI, ditherCT, ditherCP);
}

// ========== LUT sampling ==========

float3 SampleLut(float3 index) {
	float3 tex = (index + 0.5) / lutSize;
	return lutTex.Sample(smp, tex).rgb;
}

void barycentricWeight(float3 r, out float4 bary, out int3 vert2, out int3 vert3) {
	vert2 = int3(0, 0, 0); vert3 = int3(1, 1, 1);
	int3 cc = r.xyz >= r.yzx;
	bool c_xy = cc.x; bool c_yz = cc.y; bool c_zx = cc.z;
	bool c_yx = !cc.x; bool c_zy = !cc.y; bool c_xz = !cc.z;
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

// ========== SDR functions ==========

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

// ========== Vertex/Pixel shaders ==========

VS_OUTPUT VS(VS_INPUT input) {
	VS_OUTPUT output;
	output.pos = float4(input.pos, 0, 1);
	output.tex = input.tex;
	return output;
}

float4 PS(VS_OUTPUT input) : SV_TARGET {
	float3 sample = backBufferTex.Sample(smp, input.tex).rgb;

	if (colorMode == 1) {
		// HDR: scRGB linear -> Rec.2020 -> [tonemap in ICtCp] -> PQ -> LUT -> scRGB
		float3 rec2020 = mul(scrgb_to_bt2100, sample);

		if (tonemapEnabled) {
			// Rec.2020 -> LMS -> PQ -> ICtCp -> Tonemap(I) -> Dither -> reverse
			// Note: rec2020 is already normalized (10000 nits = 1.0) via scrgb_to_bt2100 matrix,
			// unlike the overlay shader where rec2020 is scRGB-scale (80 nits = 1.0).
			// No 80/10000 scaling needed here.
			float3 lms = mul(Rec2020_to_LMS, rec2020);
			float3 lmsPQ = Linear_to_PQ(lms);
			float3 ictcp = mul(LMSprime_to_ICtCp, lmsPQ);
			ictcp = ApplyTonemappingICtCp(ictcp);
			ictcp = ApplyDitherICtCp(ictcp, input.pos.xy);
			float3 lmsPQ2 = mul(ICtCp_to_LMSprime, ictcp);
			rec2020 = mul(LMS_to_Rec2020, PQ_to_Linear(lmsPQ2));
		}

		if (hasLut) {
			// PQ encode -> LUT -> PQ decode -> scRGB
			float3 pq = pq_inv_eotf(saturate(rec2020));
			float3 lut_out = LutTransformTetrahedral(pq);
			return float4(mul(bt2100_to_scrgb, pq_eotf(lut_out)), 1);
		} else {
			// No LUT — passthrough
			return float4(mul(bt2100_to_scrgb, rec2020), 1);
		}
	}
	else if (colorMode == 2) {
		// ACM SDR: scRGB linear -> sRGB encode -> LUT -> sRGB decode -> scRGB
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

// Peak detection compute shader (80x45 grid, temporal smoothing)
static const char g_peakDetectShader[] = R"(
Texture2D<float4> inputTexture : register(t0);
RWTexture2D<float> peakOutput : register(u0);

cbuffer PeakParams : register(b0) {
	uint frameWidth;
	uint frameHeight;
	float riseRate;
	float fallRate;
	float maxRisePerFrame;
	float maxFallPerFrame;
	float2 _padding;
};

groupshared float sharedMax[256];

[numthreads(256, 1, 1)]
void main(uint3 GTid : SV_GroupThreadID) {
	float localMax = 0.0;
	uint gridX = 80, gridY = 45;
	uint totalSamples = gridX * gridY;
	uint samplesPerThread = (totalSamples + 255) / 256;

	for (uint i = 0; i < samplesPerThread; i++) {
		uint sampleIdx = GTid.x * samplesPerThread + i;
		if (sampleIdx >= totalSamples) break;
		uint gx = sampleIdx % gridX;
		uint gy = sampleIdx / gridX;
		uint px = (gx * frameWidth) / gridX;
		uint py = (gy * frameHeight) / gridY;
		if (px < frameWidth && py < frameHeight) {
			float4 pixel = inputTexture.Load(int3(px, py, 0));
			float Y = dot(pixel.rgb, float3(0.2126, 0.7152, 0.0722));
			float nits = Y * 80.0;
			localMax = max(localMax, nits);
		}
	}

	sharedMax[GTid.x] = localMax;
	GroupMemoryBarrierWithGroupSync();

	for (uint stride = 128; stride > 0; stride >>= 1) {
		if (GTid.x < stride)
			sharedMax[GTid.x] = max(sharedMax[GTid.x], sharedMax[GTid.x + stride]);
		GroupMemoryBarrierWithGroupSync();
	}

	if (GTid.x == 0) {
		float framePeak = sharedMax[0];
		float prevPQ = peakOutput[uint2(0, 0)];
		float prevPeak;
		if (prevPQ <= 0.0) {
			prevPeak = framePeak;
		} else {
			float Np = pow(prevPQ, 1.0 / 78.84375);
			float L = pow(max(Np - 0.8359375, 0.0) / max(18.8515625 - 18.6875 * Np, 1e-10), 1.0 / 0.1593017578125);
			prevPeak = L * 10000.0;
		}
		float target;
		float maxDelta;
		if (framePeak > prevPeak) {
			target = lerp(prevPeak, framePeak, riseRate);
			maxDelta = maxRisePerFrame;
		} else {
			target = lerp(prevPeak, framePeak, fallRate);
			maxDelta = maxFallPerFrame;
		}
		float smoothedPeak = clamp(target, prevPeak - maxDelta, prevPeak + maxDelta);
		smoothedPeak = clamp(smoothedPeak, 0.0, 10000.0);
		float Yp = max(smoothedPeak / 10000.0, 1e-10);
		float Ym = pow(Yp, 0.1593017578125);
		peakOutput[uint2(0, 0)] = pow((0.8359375 + 18.8515625 * Ym) / (1.0 + 18.6875 * Ym), 78.84375);
	}
}
)";
