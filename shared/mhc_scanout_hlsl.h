// DesktopLUT - mhc_scanout_hlsl.h
// The MHC2 scanout stage in a shader. The live edit preview (src/mhc_preview.h) puts an identity profile on scanout
// and has a shader apply the display's profile instead, so the edit shows before it is baked. The data are the
// profile's own numbers (MhcScanout: the matrix as written, the LUT entries as written); the steps are those of
// EvalMhcScanout (src/mhc_icc.cpp), which evaluates them in double — keep the two in step. In fp32 the shader stays
// within a few 1e-7 of it in SDR and ~1e-6 in HDR (tests/test_mhc_preview.cpp, WARP), far below a 10-bit code.
// Standalone: shared by the overlay (src/shader.h) and the tests.
//
//   lin  linear-light wire RGB: SDR relative to reference white, HDR relative to 10000 nits (clamped to [0,1],
//        as the wire carries it)
//   m    the as-applied matrix (MhcScanout::applied), rows
//   lut  the three LUTs, one row per channel (R, G, B), n entries each
//   ->   the LUT output, the signal the panel receives: SDR sRGB-encoded, HDR PQ

#pragma once

#define DLUT_MHC_SCANOUT_HLSL R"(
float DlutMhcSrgbEotf(float v) { return v <= 0.04045f ? v / 12.92f : pow((v + 0.055f) / 1.055f, 2.4f); }
float DlutMhcSrgbOetf(float l) { return l <= 0.0031308f ? l * 12.92f : 1.055f * pow(l, 1.0f / 2.4f) - 0.055f; }
float DlutMhcPqEotf(float pq) {
    float vm = pow(max(pq, 1e-10f), 1.0f / 78.84375f);
    float t = max(vm - 0.8359375f, 0.0f) / max(18.8515625f - 18.6875f * vm, 1e-10f);
    return pow(t, 1.0f / 0.1593017578125f);
}
float DlutMhcPqOetf(float l) {
    float ym = pow(max(l, 0.0f), 0.1593017578125f);
    // PQ = base^78.84 with base = (c1 + c2 ym) / (1 + c3 ym) in (c1, 1]. pow() here would take log2(base) from the
    // GPU's log2, whose ~2^-22 absolute error near 1 the exponent turns into ~2e-5 of PQ. Instead: base - 1 =
    // (1 - c1)(ym - 1) / (1 + c3 ym) exactly (c2 - c3 = 1 - c1), and ln(1 + x) = 2 atanh(x / (2 + x)), whose series
    // has converged to fp32 by z^9 (|z| < 0.09 for x in (-0.165, 0]).
    float x = 0.1640625f * (ym - 1.0f) / (1.0f + 18.6875f * ym);
    float z = x / (2.0f + x);
    float z2 = z * z;
    float ln1p = 2.0f * z * (1.0f + z2 * (1.0f / 3.0f + z2 * (1.0f / 5.0f + z2 * (1.0f / 7.0f + z2 * (1.0f / 9.0f)))));
    return exp2(78.84375f * 1.44269504f * ln1p);
}
float DlutMhcLutAt(Texture2D<float> lut, int n, int ch, float v) {
    float idx = saturate(v) * (float)(n - 1);
    float i0f = floor(idx);
    int i0 = (int)i0f;
    int i1 = min(i0 + 1, n - 1);
    float t = idx - i0f;
    float a = lut.Load(int3(i0, ch, 0));
    float b = lut.Load(int3(i1, ch, 0));
    return a + (b - a) * t;
}
float3 DlutMhcScanout(float3 lin, float3x3 m, Texture2D<float> lut, int n, bool hdr) {
    float3 a = saturate(mul(m, saturate(lin)));
    float3 e;
    if (hdr) e = float3(DlutMhcPqOetf(a.x), DlutMhcPqOetf(a.y), DlutMhcPqOetf(a.z));
    else     e = float3(DlutMhcSrgbOetf(a.x), DlutMhcSrgbOetf(a.y), DlutMhcSrgbOetf(a.z));
    return float3(DlutMhcLutAt(lut, n, 0, e.x), DlutMhcLutAt(lut, n, 1, e.y), DlutMhcLutAt(lut, n, 2, e.z));
}
)"
