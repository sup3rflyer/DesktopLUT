#include "ui_gscurve.h"

#include "mhc.h"
#include "processing.h"   // ConvertColorCorrection: the runtime form the bake and the overlay read
#include <algorithm>
#include <cmath>
#include <cstdio>

namespace ui {

namespace {

float Clamp(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

std::vector<float>* Deviations(GrayscaleSettings& gs, GsChannel ch) {
    return ch == GsChannel::Gray ? nullptr : &gs.rgbDeviations[(int)ch];
}

float DeviationOf(const GrayscaleSettings& gs, int i, GsChannel ch) {
    if (ch == GsChannel::Gray) return 1.0f;
    const std::vector<float>& d = gs.rgbDeviations[(int)ch];
    return i < (int)d.size() ? d[i] : 1.0f;
}

// The point arrays sized to the point count (an empty or short curve starts at identity).
void EnsureSized(GrayscaleSettings& gs, bool hdr) {
    const int n = gs.pointCount;
    if ((int)gs.points.size() != n) {
        gs.points.resize(n);
        if (hdr) gs.initLinearPQ(); else gs.initLinear();
    }
    for (auto& d : gs.rgbDeviations)
        if ((int)d.size() != n) d.resize(n, 1.0f);
}

}  // namespace

GsCurve MakeGsCurve(const GrayscaleSettings& gs, bool hdr, float bakePeakNits) {
    GsCurve c;
    c.hdr = hdr;
    c.pqPeak = hdr ? PqOETF(Clamp(bakePeakNits, 1.0f, 10000.0f) / 10000.0f) : 0.0f;
    ColorCorrectionSettings cc;
    cc.grayscale = gs;
    cc.grayscale.enabled = true;
    c.data = ConvertColorCorrection(cc, hdr).grayscale;
    return c;
}

float GsIdentity(float t, bool hdr) { return hdr ? t : t * t; }

float GsCurvePercentAt(float t, const GsCurve& c, GsChannel ch) {
    t = Clamp(t, 0.0f, 1.0f);
    const int channel = (int)ch;
    float out;   // in point units
    if (c.hdr) {
        const float x = t * c.pqPeak;
        const float v = ch == GsChannel::Gray ? EvalGrayscaleHDR(x, c.data, c.pqPeak)
                                              : EvalGrayscaleHDR_Channel(x, c.data, c.pqPeak, channel);
        out = c.pqPeak > 0.0f ? v / c.pqPeak : 0.0f;
    } else {
        const float x = t * t;
        out = ch == GsChannel::Gray ? EvalGrayscaleSDR(x, c.data) : EvalGrayscaleSDR_Channel(x, c.data, channel);
    }
    const float identity = GsIdentity(t, c.hdr);
    if (identity <= 0.0f) {   // black: the eval returns the input (0) here, point 0 aside
        const float* pts = ch == GsChannel::Gray ? c.data.points : ch == GsChannel::R ? c.data.pointsR
                         : ch == GsChannel::G ? c.data.pointsG : c.data.pointsB;
        return pts[0] * 100.0f;
    }
    return (out / identity - 1.0f) * 100.0f;
}

GrayscaleSettings ResampleGs(const GrayscaleSettings& gs, int newCount, bool hdr, float bakePeakNits) {
    if (newCount == gs.pointCount || newCount < 2 || newCount > 32) return gs;
    GrayscaleSettings src = gs;
    EnsureSized(src, hdr);
    const GsCurve c = MakeGsCurve(src, hdr, bakePeakNits);
    GrayscaleSettings out = gs;
    out.pointCount = newCount;
    out.points.assign(newCount, 0.0f);
    for (auto& d : out.rgbDeviations) d.assign(newCount, 1.0f);
    for (int i = 0; i < newCount; i++) {
        const float t = GsSlotT(i, newCount);
        const float identity = GsIdentity(t, hdr);
        // In point units, as SetGsPointPercent stores them (black additive).
        auto valueOf = [&](GsChannel ch) {
            const float pct = GsCurvePercentAt(t, c, ch);
            return i == 0 ? pct / 100.0f : identity * (1.0f + pct / 100.0f);
        };
        const float gray = Clamp(valueOf(GsChannel::Gray), 0.0f, 1.0f);
        out.points[i] = gray;
        for (int ch = 0; ch < 3; ch++)
            out.rgbDeviations[ch][i] = gray > 1e-6f ? Clamp(valueOf((GsChannel)ch) / gray, 1.0f - kGsMaxPercent / 100.0f, 1.0f + kGsMaxPercent / 100.0f) : 1.0f;
    }
    return out;
}

float GsPointPercent(const GrayscaleSettings& gs, int i, bool hdr, GsChannel ch) {
    const int n = gs.pointCount;
    if (i < 0 || i >= n) return 0.0f;
    const float point = i < (int)gs.points.size() ? gs.points[i] : GsIdentity(GsSlotT(i, n), hdr);
    const float value = point * DeviationOf(gs, i, ch);
    if (i == 0) return value * 100.0f;   // black: additive
    const float identity = GsIdentity(GsSlotT(i, n), hdr);
    return identity > 0.0f ? (value / identity - 1.0f) * 100.0f : 0.0f;
}

bool SetGsPointPercent(GrayscaleSettings& gs, int i, bool hdr, GsChannel ch, float percent) {
    const int n = gs.pointCount;
    if (i < 0 || i >= n) return false;
    EnsureSized(gs, hdr);
    percent = Clamp(percent, i == 0 ? 0.0f : -kGsMaxPercent, kGsMaxPercent);
    const float identity = GsIdentity(GsSlotT(i, n), hdr);
    const float value = i == 0 ? percent / 100.0f : identity * (1.0f + percent / 100.0f);
    if (ch == GsChannel::Gray) {
        gs.points[i] = Clamp(value, 0.0f, 1.0f);
        return true;
    }
    if (gs.points[i] <= 1e-6f) return false;
    (*Deviations(gs, ch))[i] = Clamp(value / gs.points[i], 1.0f - kGsMaxPercent / 100.0f, 1.0f + kGsMaxPercent / 100.0f);
    return true;
}

void ResetGsPoint(GrayscaleSettings& gs, int i, bool hdr, GsChannel ch) {
    if (i < 0 || i >= gs.pointCount) return;
    EnsureSized(gs, hdr);
    if (ch == GsChannel::Gray) gs.points[i] = GsIdentity(GsSlotT(i, gs.pointCount), hdr);
    else (*Deviations(gs, ch))[i] = 1.0f;
}

bool GsHasChannelDeviations(const GrayscaleSettings& gs) {
    for (const auto& d : gs.rgbDeviations)
        for (float v : d)
            if (std::fabs(v - 1.0f) > 1e-5f) return true;
    return false;
}

void GsLumaWeights(bool hdr, float w[3]) {
    if (hdr) { w[0] = 0.2627f; w[1] = 0.6780f; w[2] = 0.0593f; }   // BT.2020 (the HDR container)
    else     { w[0] = 0.2126f; w[1] = 0.7152f; w[2] = 0.0722f; }   // Rec.709 (sRGB)
}

namespace {

// Light from a point-unit signal value: SDR the 2.2 power law, HDR PQ at the bake's peak.
float LightOf(float v, bool hdr, float pqPeak) {
    v = (std::max)(v, 0.0f);
    return hdr ? PqEOTF((std::min)(v * pqPeak, 1.0f)) : std::pow((std::min)(v, 1.0f), 2.2f);
}

float PqPeakOf(float nits) { return PqOETF(Clamp(nits, 1.0f, 10000.0f) / 10000.0f); }

constexpr float kDevLo = 1.0f - kGsMaxPercent / 100.0f, kDevHi = 1.0f + kGsMaxPercent / 100.0f;

}  // namespace

float GsBalancePercent(const GrayscaleSettings& gs, int i, bool hdr, GsChannel ch) {
    if (ch == GsChannel::Gray || i < 0 || i >= gs.pointCount) return 0.0f;
    float w[3];
    GsLumaWeights(hdr, w);
    float d[3], mean = 0.0f;
    for (int k = 0; k < 3; k++) { d[k] = DeviationOf(gs, i, (GsChannel)k); mean += w[k] * d[k]; }
    return mean > 0.0f ? (d[(int)ch] / mean - 1.0f) * 100.0f : 0.0f;
}

float GsBalancePercentAt(float t, const GsCurve& c, GsChannel ch) {
    if (ch == GsChannel::Gray) return 0.0f;
    float w[3];
    GsLumaWeights(c.hdr, w);
    float o[3], mean = 0.0f;
    for (int k = 0; k < 3; k++) { o[k] = 1.0f + GsCurvePercentAt(t, c, (GsChannel)k) / 100.0f; mean += w[k] * o[k]; }
    return mean > 0.0f ? (o[(int)ch] / mean - 1.0f) * 100.0f : 0.0f;
}

float GsPointLuminance(const GrayscaleSettings& gs, int i, bool hdr, float bakePeakNits) {
    if (i < 0 || i >= gs.pointCount || i >= (int)gs.points.size()) return 0.0f;
    float w[3];
    GsLumaWeights(hdr, w);
    const float pqPeak = PqPeakOf(bakePeakNits);
    float y = 0.0f;
    for (int k = 0; k < 3; k++) y += w[k] * LightOf(gs.points[i] * DeviationOf(gs, i, (GsChannel)k), hdr, pqPeak);
    return y;
}

bool SetGsBalancePercent(GrayscaleSettings& gs, int i, bool hdr, GsChannel ch, float percent, float bakePeakNits) {
    if (ch == GsChannel::Gray || i < 0 || i >= gs.pointCount) return false;
    EnsureSized(gs, hdr);
    const float p = gs.points[i];
    if (p <= 1e-6f) return false;
    percent = Clamp(percent, -kGsMaxPercent, kGsMaxPercent);
    float w[3];
    GsLumaWeights(hdr, w);
    const float pqPeak = PqPeakOf(bakePeakNits);
    const int a = (int)ch, b = (a + 1) % 3, e = (a + 2) % 3;   // the moved channel and the two that follow
    float d[3];
    for (int k = 0; k < 3; k++) d[k] = DeviationOf(gs, i, (GsChannel)k);
    const float y0 = GsPointLuminance(gs, i, hdr, bakePeakNits);
    // For a deviation `da` of the moved channel: the common scale k of the other two that keeps the light.
    auto followScale = [&](float da) {
        float lo = 0.0f, hi = 4.0f;
        for (int it = 0; it < 60; it++) {
            const float k = 0.5f * (lo + hi);
            const float y = w[a] * LightOf(p * da, hdr, pqPeak) + w[b] * LightOf(p * d[b] * k, hdr, pqPeak) +
                            w[e] * LightOf(p * d[e] * k, hdr, pqPeak);
            (y < y0 ? lo : hi) = k;
        }
        return 0.5f * (lo + hi);
    };
    // The balance the result shows, for a deviation `da` of the moved channel (increases with da).
    auto balanceFor = [&](float da) {
        const float k = followScale(da);
        const float mean = w[a] * da + (w[b] * d[b] + w[e] * d[e]) * k;
        return mean > 0.0f ? (da / mean - 1.0f) * 100.0f : 0.0f;
    };
    float lo = kDevLo * 0.5f, hi = kDevHi * 1.5f;
    for (int it = 0; it < 60; it++) {
        const float da = 0.5f * (lo + hi);
        (balanceFor(da) < percent ? lo : hi) = da;
    }
    const float da = 0.5f * (lo + hi);
    const float k = followScale(da);
    const float nd[3] = { a == 0 ? da : d[0] * k, a == 1 ? da : d[1] * k, a == 2 ? da : d[2] * k };
    for (float v : nd) if (v < kDevLo - 1e-4f || v > kDevHi + 1e-4f) return false;   // beyond the deviation limits
    for (int kk = 0; kk < 3; kk++) gs.rgbDeviations[kk][i] = Clamp(nd[kk], kDevLo, kDevHi);
    return true;
}

float GsPatchValue(int i, int n, bool hdr, float pqPeak, int codeCap) {
    const float t = GsSlotT(i, n);
    if (hdr) return PqEOTF(Clamp(t * pqPeak, 0.0f, 1.0f)) * 10000.0f;
    const int cap = codeCap > 0 ? codeCap : 255;
    return (float)std::lround(t * t * (float)cap) / (float)cap;
}

std::string GsPointLabel(int i, int n, bool hdr, float pqPeak, int codeCap) {
    const float t = GsSlotT(i, n);
    char buf[32];
    if (hdr) {
        const float nits = PqEOTF(t * pqPeak) * 10000.0f;
        if (nits < 1.0f) snprintf(buf, sizeof(buf), "%.2f", nits);
        else if (nits < 10.0f) snprintf(buf, sizeof(buf), "%.1f", nits);
        else snprintf(buf, sizeof(buf), "%.0f", nits);
    } else {
        snprintf(buf, sizeof(buf), "%d", (int)std::lround(t * t * (float)codeCap));
    }
    return buf;
}

}  // namespace ui
