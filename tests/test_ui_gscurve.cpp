#include "doctest.h"
#include "ui/ui_gscurve.h"
#include "mhc.h"
#include <cmath>

// ============================================================================
// Grayscale editor model (src/ui/ui_gscurve.*): the faders and the curve drawn through them. The curve
// must be the baked one (mhc.h EvalGrayscale*), pass exactly through every fader, and fader values must
// round-trip into the settings the bake reads.
// ============================================================================

using namespace ui;

namespace {

GrayscaleSettings Curve(int n, bool hdr) {
    GrayscaleSettings gs;
    gs.pointCount = n;
    gs.points.resize(n);
    if (hdr) gs.initLinearPQ(); else gs.initLinear();
    return gs;
}

// A deterministic, non-trivial correction: points and channels off identity by a few percent.
void Shape(GrayscaleSettings& gs, bool hdr) {
    for (int i = 1; i < gs.pointCount; i++) {
        SetGsPointPercent(gs, i, hdr, GsChannel::Gray, 3.0f * std::sin(0.7f * (float)i));
        SetGsPointPercent(gs, i, hdr, GsChannel::R, 2.0f * std::cos(0.5f * (float)i));
        SetGsPointPercent(gs, i, hdr, GsChannel::B, -1.5f * std::sin(0.3f * (float)i));
    }
}

}  // namespace

TEST_CASE("gs editor: the curve passes exactly through every fader, every channel, both modes") {
    for (bool hdr : { false, true }) {
        for (int n : { 10, 20, 32 }) {
            GrayscaleSettings gs = Curve(n, hdr);
            Shape(gs, hdr);
            const GsCurve c = MakeGsCurve(gs, hdr, 1612.0f);
            for (int ch = -1; ch < 3; ch++) {
                for (int i = 1; i < n; i++) {
                    CAPTURE(hdr); CAPTURE(n); CAPTURE(ch); CAPTURE(i);
                    CHECK(GsCurvePercentAt(GsSlotT(i, n), c, (GsChannel)ch) ==
                          doctest::Approx(GsPointPercent(gs, i, hdr, (GsChannel)ch)).epsilon(1e-4));
                }
            }
        }
    }
}

TEST_CASE("gs editor: between faders the curve is the bake's interpolation (SDR square-root space, HDR PQ-linear)") {
    // SDR: the bake interpolates sqrt(value) linearly in sqrt(signal); at the middle of a segment in fader
    // space (sqrt signal) the output is the squared mean of the square roots.
    GrayscaleSettings sdr = Curve(10, false);
    SetGsPointPercent(sdr, 4, false, GsChannel::Gray, 10.0f);
    const GsCurve cs = MakeGsCurve(sdr, false, 0.0f);
    const float t = (GsSlotT(4, 10) + GsSlotT(5, 10)) * 0.5f;
    const float s4 = std::sqrt(sdr.points[4]), s5 = std::sqrt(sdr.points[5]);
    const float expected = ((s4 + s5) * 0.5f) * ((s4 + s5) * 0.5f);
    CHECK(GsCurvePercentAt(t, cs, GsChannel::Gray) == doctest::Approx((expected / (t * t) - 1.0f) * 100.0f).epsilon(1e-4));
    // ...and that is exactly what the bake's evaluation returns at that signal.
    CHECK(EvalGrayscaleSDR(t * t, cs.data) == doctest::Approx(expected).epsilon(1e-5));

    // HDR: linear in PQ between the points; the percent curve does not depend on the peak the points sit at.
    GrayscaleSettings hdr = Curve(20, true);
    SetGsPointPercent(hdr, 7, true, GsChannel::Gray, -4.0f);
    const GsCurve a = MakeGsCurve(hdr, true, 1000.0f), b = MakeGsCurve(hdr, true, 4000.0f);
    for (float u : { 0.11f, 0.33f, 0.36f, 0.5f, 0.97f })
        CHECK(GsCurvePercentAt(u, a, GsChannel::Gray) == doctest::Approx(GsCurvePercentAt(u, b, GsChannel::Gray)).epsilon(1e-4));
    const float th = (GsSlotT(7, 20) + GsSlotT(8, 20)) * 0.5f;
    const float lin = (hdr.points[7] + hdr.points[8]) * 0.5f;
    CHECK(GsCurvePercentAt(th, a, GsChannel::Gray) == doctest::Approx((lin / th - 1.0f) * 100.0f).epsilon(1e-4));
}

TEST_CASE("gs editor: an identity curve is flat zero") {
    for (bool hdr : { false, true }) {
        const GsCurve c = MakeGsCurve(Curve(20, hdr), hdr, 1000.0f);
        for (float t = 0.0f; t <= 1.0f; t += 0.0137f)
            for (int ch = -1; ch < 3; ch++) CHECK(std::fabs(GsCurvePercentAt(t, c, (GsChannel)ch)) < 1e-3f);
    }
}

TEST_CASE("gs editor: fader values round-trip; black is additive; a channel at black cannot move") {
    for (bool hdr : { false, true }) {
        GrayscaleSettings gs = Curve(20, hdr);
        CHECK(SetGsPointPercent(gs, 9, hdr, GsChannel::Gray, 2.5f));
        CHECK(GsPointPercent(gs, 9, hdr, GsChannel::Gray) == doctest::Approx(2.5f).epsilon(1e-4));
        CHECK(GsPointPercent(gs, 9, hdr, GsChannel::R) == doctest::Approx(2.5f).epsilon(1e-4));   // channels ride the gray point
        CHECK(SetGsPointPercent(gs, 9, hdr, GsChannel::R, -1.0f));
        CHECK(GsPointPercent(gs, 9, hdr, GsChannel::R) == doctest::Approx(-1.0f).epsilon(1e-4));
        CHECK(GsPointPercent(gs, 9, hdr, GsChannel::Gray) == doctest::Approx(2.5f).epsilon(1e-4));  // gray untouched
        CHECK(GsHasChannelDeviations(gs));
        // Beyond the limit: clamped.
        CHECK(SetGsPointPercent(gs, 3, hdr, GsChannel::Gray, 80.0f));
        CHECK(GsPointPercent(gs, 3, hdr, GsChannel::Gray) == doctest::Approx(kGsMaxPercent).epsilon(1e-4));
        // Black: identity 0, the value is percent of full scale; a channel cannot scale a zero output.
        CHECK(SetGsPointPercent(gs, 0, hdr, GsChannel::Gray, 1.5f));
        CHECK(gs.points[0] == doctest::Approx(0.015f));
        CHECK(GsPointPercent(gs, 0, hdr, GsChannel::Gray) == doctest::Approx(1.5f));
        gs.points[0] = 0.0f;
        CHECK_FALSE(SetGsPointPercent(gs, 0, hdr, GsChannel::G, 1.0f));
        // Reset: back to identity, the gray point and a channel independently.
        ResetGsPoint(gs, 9, hdr, GsChannel::R);
        CHECK(GsPointPercent(gs, 9, hdr, GsChannel::R) == doctest::Approx(2.5f).epsilon(1e-4));
        ResetGsPoint(gs, 9, hdr, GsChannel::Gray);
        CHECK(std::fabs(GsPointPercent(gs, 9, hdr, GsChannel::Gray)) < 1e-4f);
    }
}

TEST_CASE("gs editor: point labels are the input each fader stands for") {
    // SDR: code = cap * t^2 (dense in the shadows)
    CHECK(GsPointLabel(0, 10, false, 0.0f, 255) == "0");
    CHECK(GsPointLabel(1, 10, false, 0.0f, 255) == "3");      // 255/81
    CHECK(GsPointLabel(9, 10, false, 0.0f, 255) == "255");
    CHECK(GsPointLabel(1, 10, false, 0.0f, 1023) == "13");    // 1023/81
    // HDR: nits at the bake's peak; the top fader is the peak itself
    const float pq1000 = PqOETF(1000.0f / 10000.0f);
    CHECK(GsPointLabel(19, 20, true, pq1000, 1023) == "1000");
    CHECK(GsPointLabel(0, 20, true, pq1000, 1023) == "0.00");
}

TEST_CASE("gs editor: balance is centred on gray and keeps the point's light output") {
    for (bool hdr : { false, true }) {
        GrayscaleSettings gs = Curve(20, hdr);
        SetGsPointPercent(gs, 8, hdr, GsChannel::Gray, 1.0f);
        for (int ch = 0; ch < 3; ch++) CHECK(std::fabs(GsBalancePercent(gs, 8, hdr, (GsChannel)ch)) < 1e-4f);   // neutral
        const float y0 = GsPointLuminance(gs, 8, hdr, 1612.0f);
        REQUIRE(SetGsBalancePercent(gs, 8, hdr, GsChannel::R, 2.0f, 1612.0f));
        CHECK(GsBalancePercent(gs, 8, hdr, GsChannel::R) == doctest::Approx(2.0f).epsilon(1e-3));
        CHECK(GsPointLuminance(gs, 8, hdr, 1612.0f) == doctest::Approx(y0).epsilon(1e-5));   // the light stays
        // The other two moved together, the other way, keeping their own ratio (here 1:1).
        CHECK(GsBalancePercent(gs, 8, hdr, GsChannel::G) < 0.0f);
        CHECK(gs.rgbDeviations[1][8] == doctest::Approx(gs.rgbDeviations[2][8]).epsilon(1e-5));
        // The weighted balances sum to zero: the channels move against each other.
        float w[3];
        GsLumaWeights(hdr, w);
        float sum = 0.0f;
        for (int ch = 0; ch < 3; ch++) sum += w[ch] * GsBalancePercent(gs, 8, hdr, (GsChannel)ch);
        CHECK(std::fabs(sum) < 1e-3f);
        // A second channel keeps the first's own setting relative to the third... and still the light.
        REQUIRE(SetGsBalancePercent(gs, 8, hdr, GsChannel::B, -1.5f, 1612.0f));
        CHECK(GsBalancePercent(gs, 8, hdr, GsChannel::B) == doctest::Approx(-1.5f).epsilon(1e-3));
        CHECK(GsPointLuminance(gs, 8, hdr, 1612.0f) == doctest::Approx(y0).epsilon(1e-5));
        // The gray point (the level) leaves the balance alone.
        const float bR = GsBalancePercent(gs, 8, hdr, GsChannel::R);
        SetGsPointPercent(gs, 8, hdr, GsChannel::Gray, -2.0f);
        CHECK(GsBalancePercent(gs, 8, hdr, GsChannel::R) == doctest::Approx(bR).epsilon(1e-4));
        // The balance curve between points comes from the bake's evaluation and meets the faders.
        const GsCurve c = MakeGsCurve(gs, hdr, 1612.0f);
        CHECK(GsBalancePercentAt(GsSlotT(8, 20), c, GsChannel::B) == doctest::Approx(GsBalancePercent(gs, 8, hdr, GsChannel::B)).epsilon(1e-3));
        // Black has nothing to balance.
        gs.points[0] = 0.0f;
        CHECK_FALSE(SetGsBalancePercent(gs, 0, hdr, GsChannel::R, 1.0f, 1612.0f));
    }
}

TEST_CASE("gs editor: the patch shows the input the point's label names") {
    // SDR: on the code grid (the label's code / cap), so the meter reads the very code being edited.
    CHECK(GsPatchValue(1, 10, false, 0.0f, 255) == doctest::Approx(3.0f / 255.0f));
    CHECK(GsPatchValue(9, 10, false, 0.0f, 255) == doctest::Approx(1.0f));
    CHECK(GsPatchValue(1, 10, false, 0.0f, 1023) == doctest::Approx(13.0f / 1023.0f));
    CHECK(GsPatchValue(0, 20, false, 0.0f, 1023) == 0.0f);
    // HDR: nits at the bake's peak (the top point is the peak itself).
    const float pq = PqOETF(1612.0f / 10000.0f);
    CHECK(GsPatchValue(19, 20, true, pq, 1023) == doctest::Approx(1612.0f).epsilon(1e-3));
    const float mid = GsPatchValue(10, 20, true, pq, 1023);
    CHECK(mid == doctest::Approx(PqEOTF(GsSlotT(10, 20) * pq) * 10000.0f).epsilon(1e-5));
    CHECK(std::to_string((int)std::lround(mid)) == GsPointLabel(10, 20, true, pq, 1023));
}

TEST_CASE("gs editor: a new point count keeps the curve's shape (every channel, at every new point)") {
    for (bool hdr : { false, true }) {
        CAPTURE(hdr);
        const float peak = 1612.0f;
        GrayscaleSettings gs = Curve(20, hdr);
        Shape(gs, hdr);
        SetGsPointPercent(gs, 0, hdr, GsChannel::Gray, 0.4f);   // a lifted black (additive)
        const GsCurve before = MakeGsCurve(gs, hdr, peak);
        CHECK(ResampleGs(gs, 20, hdr, peak).points == gs.points);   // same count: untouched
        for (int nn : { 10, 32 }) {
            CAPTURE(nn);
            const GrayscaleSettings out = ResampleGs(gs, nn, hdr, peak);
            REQUIRE(out.pointCount == nn);
            REQUIRE((int)out.points.size() == nn);
            for (const auto& d : out.rgbDeviations) REQUIRE((int)d.size() == nn);
            const GsCurve after = MakeGsCurve(out, hdr, peak);
            for (int i = 0; i < nn; i++) {
                const float t = GsSlotT(i, nn);
                for (GsChannel ch : { GsChannel::Gray, GsChannel::R, GsChannel::G, GsChannel::B }) {
                    CAPTURE(i);
                    CHECK(GsCurvePercentAt(t, after, ch) == doctest::Approx(GsCurvePercentAt(t, before, ch)).epsilon(1e-4));
                }
            }
        }
        // An identity curve stays identity at any count.
        const GrayscaleSettings id = ResampleGs(Curve(20, hdr), 32, hdr, peak);
        for (int i = 0; i < 32; i++)
            for (GsChannel ch : { GsChannel::Gray, GsChannel::R, GsChannel::G, GsChannel::B })
                CHECK(std::fabs(GsPointPercent(id, i, hdr, ch)) < 1e-3f);
    }
}

TEST_CASE("gs editor: each point's patch, in every unit, is the stimulus the bake places it at") {
    // HDR: PQ = t * the bake's PQ peak; nits = its EOTF; the 10-bit code is that PQ * 1023 (fractional).
    const float pq = PqOETF(1729.2629f / 10000.0f);
    const GsInput in = GsPointInput(26, 32, true, pq, 1023);
    CHECK(in.signal == doctest::Approx(26.0f / 31.0f * pq));
    CHECK(in.nits == doctest::Approx(PqEOTF(in.signal) * 10000.0f));
    CHECK(in.pq10 == doctest::Approx(in.signal * 1023.0f));
    CHECK(in.nits == doctest::Approx(GsPatchValue(26, 32, true, pq, 1023)));     // what the built-in patch shows
    // At a 10000-nit peak, 32 points land on whole 10-bit codes (1023 = 31 x 33).
    CHECK(GsPointInput(5, 32, true, 1.0f, 1023).pq10 == doctest::Approx(165.0f));
    // SDR: on the code grid at the cap; the signal is code / cap (the built-in patch's value).
    const GsInput s = GsPointInput(12, 32, false, 0.0f, 1023);
    CHECK(s.code == (int)std::lround((12.0f / 31.0f) * (12.0f / 31.0f) * 1023.0f));
    CHECK(s.signal == doctest::Approx((float)s.code / 1023.0f));
    CHECK(s.signal == doctest::Approx(GsPatchValue(12, 32, false, 0.0f, 1023)));
}

TEST_CASE("gs editor: number formatting (significant digits, a signless zero that keeps its width)") {
    CHECK(FormatSignificant(519.43f, 4) == "519.4");
    CHECK(FormatSignificant(1729.26f, 4) == "1729");
    CHECK(FormatSignificant(0.0073021f, 3) == "0.00730");
    CHECK(FormatSignificant(2.8412f, 3) == "2.84");
    CHECK(FormatSignificant(0.0f, 3) == "0");
    CHECK(FormatSigned(0.62f, 2) == "+0.62");
    CHECK(FormatSigned(-0.62f, 2) == "-0.62");
    CHECK(FormatSigned(0.0f, 2) == " 0.00");
    CHECK(FormatSigned(-0.004f, 2) == " 0.00");   // never "-0.00"
    CHECK(FormatSigned(0.004f, 2).size() == FormatSigned(0.62f, 2).size());
}
