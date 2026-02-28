#include "doctest.h"
#include "color.h"
#include <cmath>

// Standard primaries for tests
static constexpr DisplayPrimariesData kSRGB   = {0.6400f, 0.3300f, 0.3000f, 0.6000f, 0.1500f, 0.0600f, 0.3127f, 0.3290f};
static constexpr DisplayPrimariesData kBT2020 = {0.7080f, 0.2920f, 0.1700f, 0.7970f, 0.1310f, 0.0460f, 0.3127f, 0.3290f};
static constexpr DisplayPrimariesData kP3D65  = {0.6800f, 0.3200f, 0.2650f, 0.6900f, 0.1500f, 0.0600f, 0.3127f, 0.3290f};

// ============================================================================
// PQ Transfer Functions (ST.2084)
// ============================================================================

TEST_CASE("PQ: LinearToPQScalar known values") {
    // Reference: ITU-R BT.2100 Table 4
    CHECK(LinearToPQScalar(0.0f) == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(LinearToPQScalar(100.0f / 10000.0f) == doctest::Approx(0.5080f).epsilon(0.001));
    CHECK(LinearToPQScalar(1000.0f / 10000.0f) == doctest::Approx(0.7519f).epsilon(0.001));
    CHECK(LinearToPQScalar(4000.0f / 10000.0f) == doctest::Approx(0.9026f).epsilon(0.001));
    CHECK(LinearToPQScalar(1.0f) == doctest::Approx(1.0f).epsilon(0.001));
}

TEST_CASE("PQ: PQToLinearScalar known values") {
    CHECK(PQToLinearScalar(0.0f) == doctest::Approx(0.0f).epsilon(1e-6));
    CHECK(PQToLinearScalar(1.0f) == doctest::Approx(1.0f).epsilon(0.001));
}

TEST_CASE("PQ: round-trip fidelity") {
    float testValues[] = {0.001f, 0.01f, 0.1f, 0.5f, 1.0f};
    for (float x : testValues) {
        float roundTrip = PQToLinearScalar(LinearToPQScalar(x));
        CHECK(roundTrip == doctest::Approx(x).epsilon(1e-5));
    }
}

TEST_CASE("PQ: monotonicity") {
    float prev = LinearToPQScalar(0.0f);
    for (int i = 1; i <= 100; i++) {
        float x = (float)i / 100.0f;
        float pq = LinearToPQScalar(x);
        CHECK(pq > prev);
        prev = pq;
    }
}

// ============================================================================
// Primaries Matrix Calculation
// ============================================================================

static bool IsIdentity(const float m[9], float tol) {
    float identity[9] = {1,0,0, 0,1,0, 0,0,1};
    for (int i = 0; i < 9; i++) {
        if (std::fabs(m[i] - identity[i]) > tol) return false;
    }
    return true;
}

TEST_CASE("Primaries: sRGB to sRGB is identity") {
    float m[9];
    CalculatePrimariesMatrix(kSRGB, kSRGB, m);
    CHECK(IsIdentity(m, 1e-4f));
}

TEST_CASE("Primaries: BT2020 to BT2020 is identity") {
    float m[9];
    CalculatePrimariesMatrix(kBT2020, kBT2020, m);
    CHECK(IsIdentity(m, 1e-4f));
}

TEST_CASE("Primaries: sRGB to BT2020 is non-identity") {
    float m[9];
    CalculatePrimariesMatrix(kSRGB, kBT2020, m);
    CHECK_FALSE(IsIdentity(m, 0.01f));
    // Published BT.709→BT.2020 matrix reference values (row-major 3x3)
    CHECK(m[0] == doctest::Approx(0.6274f).epsilon(0.01));  // R→R
    CHECK(m[1] == doctest::Approx(0.3293f).epsilon(0.01));  // G→R
    CHECK(m[2] == doctest::Approx(0.0433f).epsilon(0.01));  // B→R
    CHECK(m[3] == doctest::Approx(0.0691f).epsilon(0.01));  // R→G
    CHECK(m[4] == doctest::Approx(0.9195f).epsilon(0.01));  // G→G
    CHECK(m[5] == doctest::Approx(0.0114f).epsilon(0.01));  // B→G
    CHECK(m[6] == doctest::Approx(0.0164f).epsilon(0.01));  // R→B
    CHECK(m[7] == doctest::Approx(0.0880f).epsilon(0.01));  // G→B
    CHECK(m[8] == doctest::Approx(0.8956f).epsilon(0.01));  // B→B

    // Verify row sums ≈ 1.0 (D65 white maps to D65 white)
    for (int row = 0; row < 3; row++) {
        float sum = m[row*3+0] + m[row*3+1] + m[row*3+2];
        CHECK(sum == doctest::Approx(1.0f).epsilon(0.01));
    }
}

TEST_CASE("Primaries: invertibility (sRGB->P3 then P3->sRGB)") {
    float m1[9], m2[9];
    CalculatePrimariesMatrix(kSRGB, kP3D65, m1);
    CalculatePrimariesMatrix(kP3D65, kSRGB, m2);

    // Product should be identity
    float product[9] = {};
    for (int i = 0; i < 3; i++)
        for (int j = 0; j < 3; j++)
            for (int k = 0; k < 3; k++)
                product[i * 3 + j] += m1[i * 3 + k] * m2[k * 3 + j];

    CHECK(IsIdentity(product, 1e-3f));
}

TEST_CASE("Primaries: singular input falls back to identity") {
    DisplayPrimariesData degenerate = {0, 0, 0, 0, 0, 0, 0.3127f, 0.3290f};
    float m[9];
    CalculatePrimariesMatrix(degenerate, kSRGB, m);
    CHECK(IsIdentity(m, 1e-6f));
}

TEST_CASE("Primaries: degenerate TARGET falls back to identity") {
    DisplayPrimariesData degenerate = {0, 0, 0, 0, 0, 0, 0.3127f, 0.3290f};
    float m[9];
    CalculatePrimariesMatrix(kSRGB, degenerate, m);
    CHECK(IsIdentity(m, 1e-6f));
}

TEST_CASE("Primaries: y near zero does not crash") {
    DisplayPrimariesData nearZero = kSRGB;
    nearZero.Ry = 0.0001f;
    float m[9];
    CalculatePrimariesMatrix(nearZero, kSRGB, m);
    // Should produce valid output without crashing
    for (int i = 0; i < 9; i++) {
        CHECK(std::isfinite(m[i]));
    }
}

// ============================================================================
// PQ Negative Input Handling
// ============================================================================

TEST_CASE("PQ: negative input clamped (LinearToPQ)") {
    float negResult = LinearToPQScalar(-1.0f);
    float floorResult = LinearToPQScalar(1e-10f);
    CHECK(negResult == doctest::Approx(floorResult).epsilon(1e-6));
    CHECK(std::isfinite(negResult));
    CHECK(negResult >= 0.0f);
}

TEST_CASE("PQ: negative input zero (PQToLinear)") {
    CHECK(PQToLinearScalar(-1.0f) == doctest::Approx(0.0f).epsilon(1e-6));
    CHECK(PQToLinearScalar(-0.001f) == doctest::Approx(0.0f).epsilon(1e-6));
}

TEST_CASE("PQ: PQToLinearScalar intermediate values") {
    CHECK(PQToLinearScalar(0.5080f) == doctest::Approx(0.01f).epsilon(0.001));
    CHECK(PQToLinearScalar(0.7519f) == doctest::Approx(0.1f).epsilon(0.001));
}

TEST_CASE("PQ: PQToLinearScalar above 1.0 clamped") {
    float result = PQToLinearScalar(1.5f);
    CHECK(std::isfinite(result));
    // PQ is defined on [0,1]; values > 1.0 should produce >= 1.0 or clamp
    CHECK(result >= 1.0f);
}

// ============================================================================
// Different White Points
// ============================================================================

TEST_CASE("Primaries: different white points produces non-identity") {
    // sRGB primaries but with D50 white point
    DisplayPrimariesData srgbD50 = {0.6400f, 0.3300f, 0.3000f, 0.6000f, 0.1500f, 0.0600f, 0.3457f, 0.3585f};
    float m[9];
    CalculatePrimariesMatrix(kSRGB, srgbD50, m);
    // Same primaries, different white point — should not be identity
    CHECK_FALSE(IsIdentity(m, 0.01f));
    // All values should be finite
    for (int i = 0; i < 9; i++) {
        CHECK(std::isfinite(m[i]));
    }
}
