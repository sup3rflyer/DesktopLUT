// HDR output dither — the ONE copy of its HLSL. The overlay pixel shader (src/shader.h) and the DWM hook
// pixel shader (dwm_hook/hook_shader.h) both splice DLUT_HDR_DITHER_HLSL into their source (like
// shared/tonemap_curves.h), so the two paths cannot drift. tests/test_hdr_dither.cpp runs this text on WARP
// against a CPU port.
//
// Where: AFTER the 3D LUT, on the PQ-encoded Rec.2020 value the shader hands to DWM — the domain of the display's
// 10-bit PQ quantizer (Windows' MHC2 stage downstream quantizes to the link, undithered; its per-channel 1D LUT
// slopes are ~0.9-1.1, so a dither sized here arrives at the real quantizer at about the right size).
// What: TPDF noise of +-1 LSB of 10-bit PQ per channel (2 LSB peak-to-peak), independent per channel, from the
// static 64x64 blue-noise tile. TPDF (not RPDF) makes the quantization error's mean AND variance independent of
// the signal, so the grain does not come and go along a gradient; +-1 LSB is the smallest TPDF that does that.
// Guard: the amplitude shrinks to the distance from 0 or 1, so exact black and the zero channels of a pure
// primary stay exactly zero — no one-sided clipping (which biased pure primaries and lifted black to code 1 in the
// old ICtCp dither), and nothing leaves [0, 1].
//
// History (2026-09-24). The old dither added blue noise in ICtCp BEFORE the 3D LUT, only while the tonemap was on:
// +-1/2 LSB on I and +-1/4 LSB on Ct/Cp. Near a saturated primary the inverse ICtCp turned the chroma noise into
// tiny R/G values that saturate() clipped to one side — up to 0.1 in PQ at the LUT input, because PQ is steep near
// zero — so the LUT's local slope decided how visible it was (a rough out-of-gamut lattice made it a static red
// speckle on bright Rec.2020 blue), primaries were biased, and nothing dithered the output when the tonemap was
// off. After the LUT, the LUT's slope no longer matters, for any user cube.
//
// Static on purpose: DWM presents only when something changes, so a per-frame pattern could not animate on a still
// desktop and would re-roll in dirty rects (caret, clock) as grain "pops"; a screen-fixed pattern under moving
// content is no more visible than on a still frame (eye tracking smears it). Estimated at 0.07-0.50 of the
// visibility threshold at 70 cm on a 4K 32" panel (Barten CSF), vs 3.3-3.6x for the undithered 10-bit contour.
//
// The HLSL uses no identifiers from either host shader except its own parameters.
#pragma once

#define DLUT_HDR_DITHER_HLSL R"HDD(
// ---- HDR output dither (one copy: shared/hdr_dither.h, rationale there) ----
// Uniform u in (0,1) -> triangular pdf on [-1, 1]. Monotone, so a blue-noise field keeps its spectrum.
float3 DlutTpdf(float3 u) {
	float3 x = 2.0 * u - 1.0;
	return sign(x) * (1.0 - sqrt(1.0 - abs(x)));
}

// TPDF dither of a PQ triple: +-lsb per channel, never leaving [0, 1] (exact 0 and 1 stay put; values outside
// [0, 1] are returned unchanged). lsb = 0 returns the input bit-for-bit.
float3 DlutDitherPQ(float3 pq, float3 u, float lsb) {
	float3 amp = clamp(min(pq, 1.0 - pq), 0.0, lsb);
	return pq + DlutTpdf(u) * amp;
}
)HDD"

// Host side, both paths: the amplitude written into the pixel shader's constant buffer. The dither rides only on a
// pass that actually PROCESSES the HDR image (LUT / tonemap / shader corrections) — a pure passthrough stays
// bit-exact — and the [General] HdrDither switch (GUI "HDR output dither") can turn it off. Fixed at 1 LSB of a
// 10-bit PQ link: the hook logs an HDR monitor whose link runs below 10 bpc (the driver usually dithers there).
inline float DlutHdrDitherLsb(bool hdr, bool processing, bool enabled) {
	return (hdr && processing && enabled) ? 1.0f / 1023.0f : 0.0f;
}
