#include "doctest.h"
#include "ipc_grayscale.h"
#include "ipc_json.h"
#include <string>

// ============================================================================
// Calibration-pipe grayscale payloads (src/ipc_grayscale.h): validated before anything is stored,
// and what is stored is what the INI loader accepts back (src/grayscale_validate.h).
// ============================================================================

using namespace ipc_json;

namespace {
JsonValue Parse(const std::string& text) {
    JsonParser p(text);
    return p.parse();
}
std::string Ramp(int n, double scale = 1.0) {   // a JSON array 0..scale
    std::string s = "[";
    for (int i = 0; i < n; i++) { if (i) s += ","; s += std::to_string(scale * i / (n - 1)); }
    return s + "]";
}
std::string Fill(int n, const char* v) {
    std::string s = "[";
    for (int i = 0; i < n; i++) { if (i) s += ","; s += v; }
    return s + "]";
}
GrayscaleSettings Sentinel() {   // a populated slot, to prove a rejected payload leaves it untouched
    GrayscaleSettings gs;
    gs.pointCount = 10;
    gs.initLinear();
    gs.points[3] = 0.5f;
    gs.enabled = false;
    return gs;
}
bool Untouched(const GrayscaleSettings& gs) {
    return gs.pointCount == 10 && gs.points.size() == 10 && gs.points[3] == 0.5f && !gs.enabled;
}
}  // namespace

TEST_CASE("IPC grayscale: a valid payload is stored as sent") {
    GrayscaleSettings gs = Sentinel();
    std::string err;
    auto p = Parse("{\"point_count\":20,\"points\":" + Ramp(20) + ",\"deviations\":{\"r\":" + Fill(20, "1.1") +
                   ",\"g\":" + Fill(20, "1") + ",\"b\":" + Fill(20, "0.9") + "}}");
    REQUIRE(ipc_grayscale::GrayscaleFromPayload(p, false, gs.pointCount, gs, err));
    CHECK(gs.enabled);
    CHECK(gs.pointCount == 20);
    REQUIRE(gs.points.size() == 20);
    CHECK(gs.points[19] == doctest::Approx(1.0f));
    CHECK(gs.rgbDeviations[0][5] == doctest::Approx(1.1f));
    CHECK(gs.rgbDeviations[2][5] == doctest::Approx(0.9f));
    CHECK(GrayscalePointsValid(gs.points, gs.pointCount));
}

TEST_CASE("IPC grayscale: no points -> the mode's identity curve, never a uniform ramp on the SDR grid") {
    std::string err;
    GrayscaleSettings sdr, hdr;
    REQUIRE(ipc_grayscale::GrayscaleFromPayload(Parse("{\"point_count\":10}"), false, 20, sdr, err));
    REQUIRE(ipc_grayscale::GrayscaleFromPayload(Parse("{\"point_count\":10}"), true, 20, hdr, err));
    // SDR point i sits at signal (i/9)^2: identity there is t^2 (a uniform ramp would be sqrt).
    CHECK(sdr.points[3] == doctest::Approx((3.0f / 9.0f) * (3.0f / 9.0f)));
    CHECK(hdr.points[3] == doctest::Approx(3.0f / 9.0f));
    // point_count absent too: the slot's current count
    GrayscaleSettings cur;
    REQUIRE(ipc_grayscale::GrayscaleFromPayload(Parse("{}"), false, 32, cur, err));
    CHECK(cur.pointCount == 32);
}

TEST_CASE("IPC grayscale: point counts other than 10/20/32 are refused, never allocated") {
    for (const char* text : { "{\"point_count\":17}", "{\"point_count\":50000000}", "{\"point_count\":-1}",
                              "{\"point_count\":2.5}", "{\"point_count\":\"20\"}" }) {
        GrayscaleSettings gs = Sentinel();
        std::string err;
        INFO(text);
        CHECK_FALSE(ipc_grayscale::GrayscaleFromPayload(Parse(text), false, 10, gs, err));
        CHECK_FALSE(err.empty());
        CHECK(Untouched(gs));
    }
}

TEST_CASE("IPC grayscale: malformed arrays are refused, naming the field, slot untouched") {
    const std::string bad[] = {
        "{\"point_count\":20,\"points\":" + Ramp(10) + "}",                         // wrong length
        "{\"point_count\":20,\"points\":" + Ramp(20, 2.5) + "}",                    // above 2
        "{\"points\":[0,\"x\",1,1,1,1,1,1,1,1]}",                                    // non-number
        "{\"point_count\":10,\"deviations\":{\"r\":" + Fill(10, "1") + "}}",         // r only
        "{\"point_count\":10,\"deviations\":{\"r\":" + Fill(10, "9") + ",\"g\":" + Fill(10, "1") +
            ",\"b\":" + Fill(10, "1") + "}}",                                        // gain above 8
        "{\"point_count\":10,\"luminance\":" + Fill(10, "-1") + "}",                // negative
        "{\"point_count\":10,\"points\":" + Ramp(10) + ",\"luminance\":" + Fill(10, "2.5") + "}",  // product > 2
        "{\"point_count\":10,\"deviations\":[1,2,3]}",                               // not an object
    };
    for (const auto& text : bad) {
        GrayscaleSettings gs = Sentinel();
        std::string err;
        INFO(text);
        CHECK_FALSE(ipc_grayscale::GrayscaleFromPayload(Parse(text), false, 10, gs, err));
        CHECK_FALSE(err.empty());
        CHECK(Untouched(gs));
    }
}

TEST_CASE("IPC grayscale: a top level a little above 1 is kept as sent; noise below 0 is clamped") {
    // DLC's touch-up solver can lift the top point past full scale (points x luminance); the bake
    // saturates it. Refusing it would abort a calibration run, clamping it would change the curve.
    GrayscaleSettings gs;
    std::string err;
    std::string pts = "[-0.00005,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,1.02]";
    REQUIRE(ipc_grayscale::GrayscaleFromPayload(Parse("{\"points\":" + pts + "}"), false, 10, gs, err));
    CHECK(gs.points[0] == 0.0f);
    CHECK(gs.points[9] == doctest::Approx(1.02f));
    CHECK(GrayscalePointsValid(gs.points, 10));   // and the INI loader keeps it
}

TEST_CASE("IPC grayscale: luminance without rgb or deviations keeps the luminance (gains 1)") {
    // Used to divide the default gains by the luminance, cancelling the main slider entirely.
    GrayscaleSettings gs;
    std::string err;
    auto p = Parse("{\"point_count\":10,\"points\":" + Ramp(10) + ",\"luminance\":" + Fill(10, "0.9") + "}");
    REQUIRE(ipc_grayscale::GrayscaleFromPayload(p, true, 10, gs, err));
    CHECK(gs.points[9] == doctest::Approx(0.9f));
    for (int ch = 0; ch < 3; ch++) CHECK(gs.rgbDeviations[ch][9] == doctest::Approx(1.0f));
}

TEST_CASE("IPC grayscale: luminance + deviations recovers the balance; luminance + rgb uses rgb") {
    std::string err;
    GrayscaleSettings a;
    auto pa = Parse("{\"point_count\":10,\"points\":" + Ramp(10) + ",\"luminance\":" + Fill(10, "0.5") +
                    ",\"deviations\":{\"r\":" + Fill(10, "0.6") + ",\"g\":" + Fill(10, "0.5") +
                    ",\"b\":" + Fill(10, "0.4") + "}}");
    REQUIRE(ipc_grayscale::GrayscaleFromPayload(pa, true, 10, a, err));
    CHECK(a.points[9] == doctest::Approx(0.5f));
    CHECK(a.rgbDeviations[0][4] == doctest::Approx(1.2f));
    CHECK(a.rgbDeviations[1][4] == doctest::Approx(1.0f));
    CHECK(a.rgbDeviations[2][4] == doctest::Approx(0.8f));

    GrayscaleSettings b;
    auto pb = Parse("{\"point_count\":10,\"points\":" + Ramp(10) + ",\"luminance\":" + Fill(10, "0.5") +
                    ",\"rgb\":{\"r\":" + Fill(10, "1.05") + ",\"g\":" + Fill(10, "1") +
                    ",\"b\":" + Fill(10, "0.95") + "}}");
    REQUIRE(ipc_grayscale::GrayscaleFromPayload(pb, true, 10, b, err));
    CHECK(b.rgbDeviations[0][4] == doctest::Approx(1.05f));
    CHECK(b.rgbDeviations[2][4] == doctest::Approx(0.95f));
}
