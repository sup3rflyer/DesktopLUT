// Peak-preserving shoulder curves (SoftClip, Reinhard) for the ICtCp I-channel tonemapper — the ONE
// copy of their HLSL. The overlay pixel shader (src/shader.h) and the DWM hook pixel shader
// (dwm_hook/hook_shader.h) both splice DLUT_TONEMAP_CURVES_HLSL into their source, so the two paths
// cannot drift apart. tests/test_tonemap_curves.cpp runs this text on WARP against a CPU port.
//
// Domain: PQ (I of ICtCp, 0..1). Knee k = 0.8 * pqTgt (0 for SDR targets <= 203 nits: full range).
//   x = I - k       overshoot above the knee
//   H = pqTgt - k   headroom (knee -> target peak)
//   S = pqSrc - k   source range (knee -> source / detected frame peak)
// Below the knee: identity. The curves engage only for S > H (source peak above target); otherwise
// identity + clip at the target (ApplyTonemappingICtCp already takes that branch for headroom <= 0).
//
// On [0, S] both curves satisfy
//   f(0) = 0 and f'(0) = 1   C1 join with the identity below the knee (no slope kink)
//   f(S) = H                 the source peak lands exactly on the target (peak preserving)
//   f' > 0, f'' < 0, f <= min(x, H)
//   f -> x as S -> H         continuous into the identity + clip branch at headroom 0
// Content above the source peak (x > S) clips at the target, as BT.2390 does. Causes: a static source
// peak set too low; the dynamic detector's rise lag / stride-4 misses; and saturated highlights — the
// detector measures PQ of BT.709 LUMINANCE (shared/peak_detect.h) while the curve maps the ICtCp I
// channel, which sits above PQ(Y) for saturated colours (~+0.03 PQ for 709 blue, +0.01 magenta), so a
// frame whose peak is set by a saturated highlight flattens that highlight's top (BT.2390: same).
// ApplyTonemappingICtCp does not apply its 3 % crossfade to these two curves: they are continuous into
// identity + clip on their own, and the crossfade would re-introduce a partial hard clip at the target.
//
// History. The original curves (to 2026-03) were k + H(1 - exp(-x/H)) and k + H x / (x + H): slope 1
// at the knee and a shape independent of the source, but the peak only approached the target
// asymptotically (target 1700: a 4000-nit peak showed at 1250, 10000 at 1436). ef0f703 (2026-03-18)
// replaced H by S in the rate: a slope kink at the knee (H/S, 0.64 at 1700/4000) and harder
// compression the brighter the frame (the peak always landed at 63 % / 50 % of the headroom, ~985
// nits at target 1700). These keep the original's knee join and add the peak constraint; both are
// >= the original curve everywhere and tend to it as S -> infinity.
//
// Reinhard: extended Reinhard in the overshoot
//   f(x) = x / (1 + c x),  c = 1/H - 1/S = (S - H) / (S H)
//   f'(x) = 1 / (1 + c x)^2  ->  f'(0) = 1;   f(S) = S / (1 + S/H - 1) = H;   f'' = -2c / (1 + c x)^3 < 0
//   S -> H: c -> 0, f -> x.   S -> inf: f -> x / (1 + x/H), the original curve.
//
// SoftClip: normalised exponential
//   f(x) = H (1 - exp(-a x)) / (1 - exp(-a S))
//   f(S) = H by construction. f'(0) = a H / (1 - exp(-a S)) = 1  <=>  a H = 1 - exp(-a S).
//   With u = a S, r = H/S in (0, 1), d = 1 - r = (S - H)/S:   g(u) = (1 - exp(-u)) / u = r.
//   g falls strictly from 1 (u -> 0) to 0 (u -> inf), so there is exactly one root u* > 0 iff S > H,
//   and 2d <= u* < 1/r, i.e. a = u*/S in (0, 1/H)
//   [lower: u - 1 + exp(-u) <= u^2/2 gives d = 1 - g(u*) <= u*/2;  upper: r u* = 1 - exp(-u*) < 1].
//   S -> H: u* ~ 2d -> 0 and f -> x.   S -> inf: a -> 1/H and f -> H (1 - exp(-x/H)), the original.
//   The profile t = x/S -> H E(u t) / E(u) (E(y) = 1 - exp(-y)) depends only on r, and u is a
//   per-frame constant; it is solved per pixel (only pixels above the knee pay) with a fixed cost:
//     start  u0 = d (1 + r + r d / 3) / r   — 2d + 4/3 d^2 + O(d^3) as d -> 0 (the series of the
//            root), -> 1/r as r -> 0; within 1.71 % of u* over all of (0, 1) and always above the
//            maximum of phi at u = -ln r;
//     then two Newton steps on  phi(u) = d u - (u - E(u))   (= E(u) - r u, written so the d -> 0 end
//            does not cancel), phi'(u) = d - E(u). phi is concave and u0 lies past its maximum, so
//            Newton converges without oscillating; two steps leave < 4e-10 relative error in exact
//            arithmetic. In float the knee slope is 1 within 1e-6 for r >= 0.05 (every reachable
//            setting: targets are clamped to >= 10 nits, so r >= ~0.2); it degrades only for r ~ 1e-3.
//            Each step is clamped to the bracket [2d, 1/r] against float noise.
//   E(y) uses a 7-term Taylor series below y = 1/8 (truncation < 1.3e-11 relative, the size of the
//   step at the hand-off), because 1 - exp(-y) cancels as y -> 0, which is exactly the S -> H end.
//   r and d are passed separately (each computed without cancellation) and never re-derived from
//   each other: GPU division may round H/S to exactly 1 when S is 1 ulp above H, and d > 0 must still
//   drive the solve (u >= 2d > 0, so no 0/0). d <= 0 falls back to identity + clip.
//   Guard: r is floored at 1e-4 (H < 1e-4 S, i.e. a target below ~1e-6 nits — unreachable, defensive).
//
// Cost per pixel above the knee: SoftClip 4 exp + ~50 ALU, Reinhard 1 division (below the knee both
// return early — unless the compiler flattens the branch, which still costs only that much). With the
// source peak at or below the target, ApplyTonemappingICtCp never calls them (uniform branch).
//
// The HLSL uses no identifiers from either host shader except its own parameters (the hook's
// cbuffer has pqSourcePeak / pqTargetPeak and globals c1..c3 — do not use those names here).
#pragma once

#define DLUT_TONEMAP_CURVES_HLSL R"TMC(
// ---- Peak-preserving SoftClip / Reinhard (one copy: shared/tonemap_curves.h, derivation there) ----
// Knee 0.8*pqTgt (0 for SDR targets <= 203 nits). On [knee, src peak]: slope 1 at the knee, the source
// peak maps exactly to the target, monotone, concave, never above the target. Above the source peak:
// clip at the target. Source peak <= target: identity + clip.

// 1 - exp(-y) for y >= 0 without the cancellation near 0 (7-term Taylor below 1/8)
float TonemapOneMinusExpNeg(float y) {
	float series = y * (1.0 - (1.0 / 2.0) * y * (1.0 - (1.0 / 3.0) * y * (1.0 - (1.0 / 4.0) * y *
	               (1.0 - (1.0 / 5.0) * y * (1.0 - (1.0 / 6.0) * y * (1.0 - (1.0 / 7.0) * y))))));
	return (y < 0.125) ? series : 1.0 - exp(-y);
}

// SoftClip rate u = a*S: the root of (1 - exp(-u)) / u = r, for r = H/S and d = (S - H)/S, d > 0
float TonemapSoftClipRate(float r, float d) {
	r = max(r, 1e-4);
	float u = d * (1.0 + r + r * d * (1.0 / 3.0)) / r;
	[unroll] for (int i = 0; i < 2; i++) {
		float e = TonemapOneMinusExpNeg(u);
		u = clamp(u - (d * u - (u - e)) / (d - e), 2.0 * d, 1.0 / r);
	}
	return u;
}

// SoftClip - PQ native: normalised exponential shoulder k + H (1 - exp(-a x)) / (1 - exp(-a S))
float TonemapSoftClip_PQ(float I, float pqSrcPeak, float pqTgtPeak, float targetNits) {
	float pqKnee = (targetNits <= 203.0) ? 0.0 : pqTgtPeak * 0.8;
	if (I <= pqKnee) return I;
	float H = pqTgtPeak - pqKnee;
	float S = pqSrcPeak - pqKnee;
	float d = (pqSrcPeak - pqTgtPeak) / S;
	if (S <= H || H <= 0.0 || d <= 0.0) return min(I, pqTgtPeak);
	float u = TonemapSoftClipRate(H / S, d);
	float t = (I - pqKnee) / S;
	return min(pqKnee + H * TonemapOneMinusExpNeg(u * t) / TonemapOneMinusExpNeg(u), pqTgtPeak);
}

// Reinhard - PQ native: extended Reinhard shoulder k + x / (1 + x (S - H) / (S H))
float TonemapReinhard_PQ(float I, float pqSrcPeak, float pqTgtPeak, float targetNits) {
	float pqKnee = (targetNits <= 203.0) ? 0.0 : pqTgtPeak * 0.8;
	if (I <= pqKnee) return I;
	float H = pqTgtPeak - pqKnee;
	float S = pqSrcPeak - pqKnee;
	if (S <= H || H <= 0.0) return min(I, pqTgtPeak);
	float x = I - pqKnee;
	return min(pqKnee + x / (1.0 + x * (pqSrcPeak - pqTgtPeak) / (S * H)), pqTgtPeak);
}
)TMC"
