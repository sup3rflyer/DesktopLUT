#pragma once
// The grayscale curve as the interface's editor draws and edits it: evenly spaced faders, one per point,
// with the curve between them evaluated by the bake's own functions (mhc.h EvalGrayscale*), so what is
// drawn is what is baked. No ImGui, no Windows: tested in tests/test_ui_gscurve.cpp.
//
// The editor's horizontal axis is the point index, t = i/(n-1): SDR points sit at signal t² (code cap·t²,
// dense in the shadows), HDR points at PQ t·pqPeak. Values are percent away from identity (output = input):
// relative for every point above black; point 0 (black, identity 0) is additive, percent of full scale.
// A channel's value is its own output (the gray point times the channel's deviation).

#include "types.h"
#include <string>

namespace ui {

enum class GsChannel { Gray = -1, R = 0, G = 1, B = 2 };

constexpr float kGsMaxPercent = 50.0f;   // values beyond this are clamped (as the classic editor's typed limit)

struct GsCurve {
    bool hdr = false;
    float pqPeak = 0.0f;    // HDR: the PQ signal the top point sits at (the bake's placement)
    GrayscaleData data;     // the runtime form the bake evaluates (points + per-channel points)
};
// `bakePeakNits`: the peak the HDR bake places the points by (MhcProfileMetadataPeakNits).
GsCurve MakeGsCurve(const GrayscaleSettings& gs, bool hdr, float bakePeakNits);

inline float GsSlotT(int i, int n) { return n > 1 ? (float)i / (float)(n - 1) : 0.0f; }
float GsIdentity(float t, bool hdr);                     // identity output at t, in point units (SDR t², HDR t)
// The curve's value at t (0..1) in percent, exactly as the bake evaluates it.
float GsCurvePercentAt(float t, const GsCurve& c, GsChannel ch);

// A point's value as its fader shows it, and back. Set returns false when the point cannot carry it (a
// channel of point 0 while the gray point is at black: no output to scale). Values clamp to ±kGsMaxPercent.
// The curve with `newCount` points (10 / 20 / 32), its shape kept: every new point takes the value the current
// curve has there (each channel, the bake's own interpolation), so changing the count does not reset the edit.
GrayscaleSettings ResampleGs(const GrayscaleSettings& gs, int newCount, bool hdr, float bakePeakNits);

float GsPointPercent(const GrayscaleSettings& gs, int i, bool hdr, GsChannel ch);
bool SetGsPointPercent(GrayscaleSettings& gs, int i, bool hdr, GsChannel ch, float percent);
// The point back at identity (gray: the point; a channel: its deviation to 1).
void ResetGsPoint(GrayscaleSettings& gs, int i, bool hdr, GsChannel ch);
// Whether any channel deviates anywhere (the R/G/B curves are worth drawing).
bool GsHasChannelDeviations(const GrayscaleSettings& gs);

// ---- RGB balance: the channels centred on the gray point, moving against each other ----------------
// The gray point sets the level; a channel's balance is its output relative to the point's luminance-weighted
// mean of the three (percent, 0 = neutral). Setting one channel's balance scales the other two together
// (keeping their own ratio) so the point's light output stays exactly where it was: luminance through the
// mode's EOTF (SDR 2.2 power, HDR PQ at the bake's peak) with the mode's weights (SDR Rec.709, HDR BT.2020).
void GsLumaWeights(bool hdr, float w[3]);
float GsBalancePercent(const GrayscaleSettings& gs, int i, bool hdr, GsChannel ch);
// The balance curve between points (from the bake's per-channel evaluation).
float GsBalancePercentAt(float t, const GsCurve& c, GsChannel ch);
// False at black (no output to balance) or when the balance cannot be reached within the deviation limits.
bool SetGsBalancePercent(GrayscaleSettings& gs, int i, bool hdr, GsChannel ch, float percent, float bakePeakNits);
// The point's light output (relative luminance, 0..1 at full scale): what a balance edit keeps.
float GsPointLuminance(const GrayscaleSettings& gs, int i, bool hdr, float bakePeakNits);

// The input a point stands for: SDR the code at `codeCap` (255 / 1023), HDR nits at the bake's peak.
std::string GsPointLabel(int i, int n, bool hdr, float pqPeak, int codeCap);
// What a measurement patch shows for point i: HDR its nits; SDR its signal on the code grid (code / cap), the
// same input the label names.
float GsPatchValue(int i, int n, bool hdr, float pqPeak, int codeCap);

}  // namespace ui
