// Peak-preserving SoftClip / Reinhard tonemap curves (shared/tonemap_curves.h) — the one HLSL copy both
// the overlay (src/shader.h) and the DWM hook (dwm_hook/hook_shader.h) splice into their pixel shaders.
//
// Three layers:
//   1. CPU port of the exact HLSL (same algorithm and constants, templated on precision): knee slope 1,
//      source peak -> target, monotone, concave, <= target, continuity as the source peak falls to the
//      target, ordering against the two historical curves, and pinned values for a 1700-nit target
//      from an independent reference (exact root by bracketing, not the shader's Newton solve).
//   2. The REAL HLSL text (DLUT_TONEMAP_CURVES_HLSL) in a compute shader on WARP vs the CPU port.
//      Skips (with a message) only if no D3D11 device can be created.
//   3. Both production pixel shaders compile with the shared text spliced in, and carry it verbatim.
//
// Keep TonemapCpu:: in lockstep with shared/tonemap_curves.h (and ApplyIChannel with
// ApplyTonemappingICtCp in both shaders) — layer 2 fails if the HLSL drifts from this port.

#define _CRT_SECURE_NO_WARNINGS  // dwm_hook/hook_log.h (via hook_shader.h) calls fopen; /sdl makes C4996 an error
#define NOMINMAX
#include <windows.h>             // hook_log.h needs MAX_PATH / ExpandEnvironmentStringsA

#include "doctest.h"
#include "../shared/tonemap_curves.h"
#include "shader.h"                     // g_psSource (overlay)
#include "../dwm_hook/hook_shader.h"    // g_shaders (DWM hook)

#include <d3d11.h>
#include <d3dcompiler.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

namespace {

// ---------------------------------------------------------------------------------------------
// Double-precision PQ (ST.2084) for the test's own nits <-> PQ conversions
// ---------------------------------------------------------------------------------------------
constexpr double kM1 = 2610.0 / 16384.0, kM2 = 2523.0 / 4096.0 * 128.0;
constexpr double kC1 = 3424.0 / 4096.0, kC2 = 2413.0 / 4096.0 * 32.0, kC3 = 2392.0 / 4096.0 * 32.0;

double PQ(double nits) {
    double y = std::pow((std::max)(nits, 0.0) / 10000.0, kM1);
    return std::pow((kC1 + kC2 * y) / (1.0 + kC3 * y), kM2);
}
double Nits(double pq) {
    double p = std::pow((std::max)(pq, 0.0), 1.0 / kM2);
    return 10000.0 * std::pow((std::max)(p - kC1, 0.0) / (kC2 - kC3 * p), 1.0 / kM1);
}

// ---------------------------------------------------------------------------------------------
// CPU port of shared/tonemap_curves.h — line for line, same constants
// ---------------------------------------------------------------------------------------------
namespace TonemapCpu {

template <typename R> R OneMinusExpNeg(R y) {
    R series = y * (R(1) - (R(1) / R(2)) * y * (R(1) - (R(1) / R(3)) * y * (R(1) - (R(1) / R(4)) * y *
               (R(1) - (R(1) / R(5)) * y * (R(1) - (R(1) / R(6)) * y * (R(1) - (R(1) / R(7)) * y))))));
    return (y < R(0.125)) ? series : R(1) - std::exp(-y);
}

template <typename R> R SoftClipRate(R H, R S, R d) {
    R r = (std::max)(H / S, R(1e-4));
    d = (std::min)(d, R(1) - r);
    R u = d * (R(1) + r + r * d * (R(1) / R(3))) / r;
    for (int i = 0; i < 2; i++) {
        R e = OneMinusExpNeg(u);
        u = std::clamp(u - (d * u - (u - e)) / (d - e), R(2) * d, R(1) / r);
    }
    return u;
}

template <typename R> R SoftClip(R I, R pqSrcPeak, R pqTgtPeak, R targetNits) {
    R pqKnee = (targetNits <= R(203)) ? R(0) : pqTgtPeak * R(0.8);
    if (I <= pqKnee) return I;
    R H = pqTgtPeak - pqKnee;
    R S = pqSrcPeak - pqKnee;
    if (S <= H || H <= R(0)) return (std::min)(I, pqTgtPeak);
    R u = SoftClipRate(H, S, (pqSrcPeak - pqTgtPeak) / S);
    R t = (I - pqKnee) / S;
    return (std::min)(pqKnee + H * OneMinusExpNeg(u * t) / OneMinusExpNeg(u), pqTgtPeak);
}

template <typename R> R Reinhard(R I, R pqSrcPeak, R pqTgtPeak, R targetNits) {
    R pqKnee = (targetNits <= R(203)) ? R(0) : pqTgtPeak * R(0.8);
    if (I <= pqKnee) return I;
    R H = pqTgtPeak - pqKnee;
    R S = pqSrcPeak - pqKnee;
    if (S <= H || H <= R(0)) return (std::min)(I, pqTgtPeak);
    R x = I - pqKnee;
    return (std::min)(pqKnee + x / (R(1) + x * (pqSrcPeak - pqTgtPeak) / (S * H)), pqTgtPeak);
}

} // namespace TonemapCpu

using Curve = std::function<double(double, double, double, double)>;

double NewSoftClip(double I, double s, double t, double n) { return TonemapCpu::SoftClip<double>(I, s, t, n); }
double NewReinhard(double I, double s, double t, double n) { return TonemapCpu::Reinhard<double>(I, s, t, n); }

// Historical curves, for the ordering checks and the table only (never shipped again)
double KneeOf(double tgt, double nits) { return nits <= 203.0 ? 0.0 : 0.8 * tgt; }
double PreMarchSoftClip(double I, double, double tgt, double n) {   // to 2026-03: rate 1/H, asymptote H
    double k = KneeOf(tgt, n); if (I <= k) return I;
    double H = tgt - k; return k + H * (1.0 - std::exp(-(I - k) / H));
}
double PreMarchReinhard(double I, double, double tgt, double n) {
    double k = KneeOf(tgt, n); if (I <= k) return I;
    double H = tgt - k, x = I - k; return k + H * x / (x + H);
}
double Ef0f703SoftClip(double I, double src, double tgt, double n) {  // 2026-03-18 .. this change: rate 1/S
    double k = KneeOf(tgt, n); if (I <= k) return I;
    double H = tgt - k, S = src - k; return k + H * (1.0 - std::exp(-(I - k) / S));
}
double Ef0f703Reinhard(double I, double src, double tgt, double n) {
    double k = KneeOf(tgt, n); if (I <= k) return I;
    double H = tgt - k, S = src - k, x = I - k; return k + H * x / (x + S);
}

// ApplyTonemappingICtCp's I-channel wrapper (identical in both shaders): source <= target -> clip,
// then the 3 % PQ crossfade towards min(I, target) while the source barely exceeds the target.
double ApplyIChannel(const Curve& curve, double I, double src, double tgt, double nits) {
    if (I <= 0.0) return I;
    double headroom = src - tgt;
    double margin = tgt * 0.03;
    if (headroom <= 0.0) return (std::min)(I, tgt);
    double m = curve(I, src, tgt, nits);
    if (headroom < margin) {
        double b = headroom / margin;
        double clipped = (std::min)(I, tgt);
        m = clipped + b * (m - clipped);
    }
    return m;
}

// Independent SoftClip rate: bracketing solve of (1 - exp(-u)) / u = r on [2d, 1/r]
double ExactSoftClipRate(double r) {
    double d = 1.0 - r;
    double lo = 2.0 * d * (1.0 - 1e-12), hi = 1.0 / r;
    for (int i = 0; i < 200; i++) {
        double mid = 0.5 * (lo + hi);
        if (-std::expm1(-mid) / mid > r) lo = mid; else hi = mid;
    }
    return 0.5 * (lo + hi);
}

// The (target, source peak) grid the property tests sweep. SDR targets (<= 203) have knee 0.
struct Case { double tgtNits, srcNits; };
std::vector<Case> CaseGrid() {
    std::vector<Case> cases;
    for (double tgt : { 100.0, 203.0, 204.0, 400.0, 600.0, 1000.0, 1700.0, 4000.0 }) {
        for (double f : { 1.0001, 1.001, 1.01, 1.1, 1.5, 2.0, 4.0 })
            if (tgt * f <= 10000.0) cases.push_back({ tgt, tgt * f });
        cases.push_back({ tgt, 10000.0 });
    }
    return cases;
}

// Right derivative at the knee by Richardson-extrapolated forward differences
double KneeSlope(const Curve& f, double src, double tgt, double nits) {
    double k = KneeOf(tgt, nits);
    double h = 1e-5 * (src - k);
    double s1 = (f(k + h, src, tgt, nits) - k) / h;
    double s2 = (f(k + 0.5 * h, src, tgt, nits) - k) / (0.5 * h);
    return 2.0 * s2 - s1;
}

// ---------------------------------------------------------------------------------------------
// WARP harness: DLUT_TONEMAP_CURVES_HLSL in a compute shader, one float4 (I, src, tgt, nits) in,
// (SoftClip, Reinhard) out, per thread
// ---------------------------------------------------------------------------------------------
const char* const kCurvesTestCS =
    "RWStructuredBuffer<float4> io : register(u0);\n"
    DLUT_TONEMAP_CURVES_HLSL
    R"(
[numthreads(64, 1, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    float4 v = io[id.x];
    io[id.x] = float4(TonemapSoftClip_PQ(v.x, v.y, v.z, v.w), TonemapReinhard_PQ(v.x, v.y, v.z, v.w), 0.0, 0.0);
}
)";

struct CurvesGpu {
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* dc = nullptr;
    ID3D11ComputeShader* cs = nullptr;
    std::string error;
    bool deviceUnavailable = false;

    ~CurvesGpu() {
        if (cs) cs->Release();
        if (dc) dc->Release();
        if (device) device->Release();
    }

    bool Init() {
        D3D_FEATURE_LEVEL fl;
        HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, nullptr, 0,
                                       D3D11_SDK_VERSION, &device, &fl, &dc);
        if (FAILED(hr))
            hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0,
                                   D3D11_SDK_VERSION, &device, &fl, &dc);
        if (FAILED(hr)) { deviceUnavailable = true; error = "no D3D11 device (WARP or hardware)"; return false; }
        if (fl < D3D_FEATURE_LEVEL_11_0) { deviceUnavailable = true; error = "feature level < 11_0"; return false; }

        ID3DBlob* blob = nullptr;
        ID3DBlob* err = nullptr;
        // Same compile flags as the production shaders (gpu.cpp / hook_render.cpp: 0)
        hr = D3DCompile(kCurvesTestCS, strlen(kCurvesTestCS), "TonemapCurvesCS", nullptr, nullptr, "main", "cs_5_0",
                        0, 0, &blob, &err);
        if (FAILED(hr)) {
            error = std::string("compile failed: ") + (err ? (const char*)err->GetBufferPointer() : "?");
            if (err) err->Release();
            return false;
        }
        if (err) err->Release();
        hr = device->CreateComputeShader(blob->GetBufferPointer(), blob->GetBufferSize(), nullptr, &cs);
        blob->Release();
        if (FAILED(hr)) { error = "CreateComputeShader failed"; return false; }
        return true;
    }

    // in: (I, src, tgt, nits) per element; returns (SoftClip, Reinhard) per element
    std::vector<std::pair<float, float>> Run(std::vector<float> packed) {
        size_t n = packed.size() / 4;
        size_t padded = (n + 63) / 64 * 64;
        packed.resize(padded * 4, 0.0f);

        D3D11_BUFFER_DESC bd = {};
        bd.ByteWidth = (UINT)(padded * 16);
        bd.Usage = D3D11_USAGE_DEFAULT;
        bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
        bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
        bd.StructureByteStride = 16;
        D3D11_SUBRESOURCE_DATA init = { packed.data(), 0, 0 };
        ID3D11Buffer* buf = nullptr;
        REQUIRE(SUCCEEDED(device->CreateBuffer(&bd, &init, &buf)));

        D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
        ud.Format = DXGI_FORMAT_UNKNOWN;
        ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
        ud.Buffer.NumElements = (UINT)padded;
        ID3D11UnorderedAccessView* uav = nullptr;
        REQUIRE(SUCCEEDED(device->CreateUnorderedAccessView(buf, &ud, &uav)));

        D3D11_BUFFER_DESC sd = bd;
        sd.Usage = D3D11_USAGE_STAGING;
        sd.BindFlags = 0;
        sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        sd.MiscFlags = 0;
        ID3D11Buffer* staging = nullptr;
        REQUIRE(SUCCEEDED(device->CreateBuffer(&sd, nullptr, &staging)));

        dc->CSSetShader(cs, nullptr, 0);
        dc->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
        dc->Dispatch((UINT)(padded / 64), 1, 1);
        ID3D11UnorderedAccessView* nullUav = nullptr;
        dc->CSSetUnorderedAccessViews(0, 1, &nullUav, nullptr);
        dc->CSSetShader(nullptr, nullptr, 0);
        dc->CopyResource(staging, buf);

        std::vector<std::pair<float, float>> out(n);
        D3D11_MAPPED_SUBRESOURCE m = {};
        REQUIRE(SUCCEEDED(dc->Map(staging, 0, D3D11_MAP_READ, 0, &m)));  // blocks until the GPU is done
        const float* p = (const float*)m.pData;
        for (size_t i = 0; i < n; i++) out[i] = { p[i * 4 + 0], p[i * 4 + 1] };
        dc->Unmap(staging, 0);
        staging->Release();
        uav->Release();
        buf->Release();
        return out;
    }
};

} // namespace

// =============================================================================================
// CPU port: the analytic properties
// =============================================================================================

TEST_CASE("Tonemap curves: SoftClip rate solve hits the exact root of (1 - exp(-u))/u = H/S") {
    std::vector<double> ds;
    for (int i = 0; i <= 60; i++) ds.push_back(std::pow(10.0, -7.0 + 6.0 * i / 60.0));  // 1e-7 .. 0.1
    for (int i = 0; i <= 900; i++) ds.push_back(0.1 + 0.8999 * i / 900.0);              // 0.1 .. 0.9999
    for (double d : ds) {
        double r = 1.0 - d;
        double exact = ExactSoftClipRate(r);
        double u = TonemapCpu::SoftClipRate<double>(r, 1.0, d);  // scale-free: only H/S matters
        INFO("d = " << d << "  u = " << u << "  exact = " << exact);
        CHECK(std::fabs(u - exact) <= 1e-9 * exact + 1e-15);
        CHECK(u >= 2.0 * d);        // bracket: a in (0, 1/H)
        CHECK(u <= 1.0 / r);
        // f'(0) = r u / (1 - exp(-u)) = 1
        CHECK(std::fabs(r * u / -std::expm1(-u) - 1.0) < 1e-9);

        // float solve (what the GPU runs): the knee slope it implies is still 1 to ~1e-6
        float uf = TonemapCpu::SoftClipRate<float>((float)r, 1.0f, (float)d);
        CHECK(std::isfinite(uf));
        CHECK(std::fabs(r * (double)uf / -std::expm1(-(double)uf) - 1.0) < 1e-5);
    }
}

TEST_CASE("Tonemap curves: slope 1 at the knee, source peak -> target, monotone, concave, <= target") {
    const std::pair<std::string, Curve> curves[] = { { "SoftClip", NewSoftClip }, { "Reinhard", NewReinhard } };
    for (const auto& [name, f] : curves) {
        for (const Case& c : CaseGrid()) {
            double tgt = PQ(c.tgtNits), src = PQ(c.srcNits), k = KneeOf(tgt, c.tgtNits);
            INFO(name << "  target " << c.tgtNits << "  source peak " << c.srcNits);

            // identity below the knee
            for (double I : { 0.25 * k, 0.5 * k, k })
                CHECK(f(I, src, tgt, c.tgtNits) == I);

            // C1 at the knee (below it the slope is 1)
            CHECK(std::fabs(KneeSlope(f, src, tgt, c.tgtNits) - 1.0) < 1e-6);

            // the source peak lands on the target; above it, clip
            CHECK(std::fabs(f(src, src, tgt, c.tgtNits) - tgt) < 1e-12);
            CHECK(f(src + 0.5 * (1.0 - src) + 1e-9, src, tgt, c.tgtNits) == tgt);

            // sweep over [knee, source peak]: monotone, concave, <= identity, <= target. Concavity by
            // second differences, allowing 1e-11 PQ of value noise (SoftClip's series -> exp hand-off
            // in 1 - exp(-y) leaves < 2e-12); a real convex bump would be orders of magnitude larger.
            const int N = 400;
            double prev2 = 0.0, prev = k;
            bool monotone = true, concave = true, belowIdentity = true, belowTarget = true;
            for (int i = 1; i <= N; i++) {
                double I = k + (src - k) * i / N;
                double v = f(I, src, tgt, c.tgtNits);
                if (v < prev) monotone = false;
                if (i >= 2 && v - 2.0 * prev + prev2 > 1e-11) concave = false;
                if (v > I + 1e-12) belowIdentity = false;
                if (v > tgt) belowTarget = false;
                prev2 = prev; prev = v;
            }
            // first step: slope <= 1 (the identity's) — the concave join at the knee
            CHECK(f(k + (src - k) / N, src, tgt, c.tgtNits) - k <= (src - k) / N + 1e-15);
            CHECK(monotone);
            CHECK(concave);
            CHECK(belowIdentity);
            CHECK(belowTarget);
        }
    }
}

TEST_CASE("Tonemap curves: continuous into identity + clip as the source peak falls to the target") {
    const std::pair<std::string, Curve> curves[] = { { "SoftClip", NewSoftClip }, { "Reinhard", NewReinhard } };
    for (const auto& [name, f] : curves) {
        for (double tgtNits : { 100.0, 400.0, 1000.0, 1700.0 }) {
            double tgt = PQ(tgtNits);
            for (double eps : { 1e-2, 1e-3, 1e-4, 1e-5, 1e-6, 1e-8, 0.0 }) {
                double src = tgt + eps;
                INFO(name << "  target " << tgtNits << "  source - target = " << eps << " PQ");
                double worstCurve = 0.0, worstWrapped = 0.0;
                for (int i = 0; i <= 2000; i++) {
                    double I = (src + 0.02) * i / 2000.0;
                    double ref = (std::min)(I, tgt);
                    worstCurve = (std::max)(worstCurve, std::fabs(f(I, src, tgt, tgtNits) - ref));
                    worstWrapped = (std::max)(worstWrapped, std::fabs(ApplyIChannel(f, I, src, tgt, tgtNits) - ref));
                }
                // the curve itself is within O(source - target) of identity + clip (no margin blend needed)
                CHECK(worstCurve <= 2.0 * eps + 1e-12);
                CHECK(worstWrapped <= 2.0 * eps + 1e-12);
            }
        }
    }
}

TEST_CASE("Tonemap curves: never darker than the pre-2026-03 curve, which is never darker than ef0f703") {
    const struct { std::string name; Curve now, preMarch, ef0f703; } families[] = {
        { "SoftClip", NewSoftClip, PreMarchSoftClip, Ef0f703SoftClip },
        { "Reinhard", NewReinhard, PreMarchReinhard, Ef0f703Reinhard },
    };
    for (const auto& fam : families) {
        for (const Case& c : CaseGrid()) {
            double tgt = PQ(c.tgtNits), src = PQ(c.srcNits), k = KneeOf(tgt, c.tgtNits);
            INFO(fam.name << "  target " << c.tgtNits << "  source peak " << c.srcNits);
            bool ordered = true;
            for (int i = 0; i <= 1000; i++) {
                double I = k + (src - k) * i / 1000.0;
                double a = fam.now(I, src, tgt, c.tgtNits);
                double b = fam.preMarch(I, src, tgt, c.tgtNits);
                double e = fam.ef0f703(I, src, tgt, c.tgtNits);
                if (a < b - 1e-12 || b < e - 1e-12) ordered = false;
            }
            CHECK(ordered);
        }
    }
}

TEST_CASE("Tonemap curves: target 1700 dynamic, displayed nits pinned against an independent reference") {
    // Dynamic mode: source peak = max(detected frame peak, target) (SoftClip/Reinhard floor = target),
    // through ApplyTonemappingICtCp's wrapper (3 % PQ crossfade included: it is active at 1800/2000).
    // Reference values: exact SoftClip root by bracketing (Python/scipy brentq), double PQ, 2026-09-23.
    const double tgtNits = 1700.0, tgt = PQ(tgtNits);
    const double content[] = { 400.0, 700.0, 1000.0, 1500.0, 1700.0, -1.0 /* = frame peak */ };
    struct Row { double peak; double softClip[6]; double reinhard[6]; };
    const Row rows[] = {
        { 1800.0,  { 399.995, 698.427, 994.347, 1482.956, 1677.024, 1700.0 },
                   { 399.995, 698.415, 994.320, 1482.932, 1677.014, 1700.0 } },
        { 2000.0,  { 399.963, 688.619, 959.906, 1382.434, 1543.047, 1700.0 },
                   { 399.962, 688.379, 959.371, 1381.817, 1542.590, 1700.0 } },
        { 4000.0,  { 399.834, 652.707, 844.455, 1083.528, 1160.513, 1700.0 },
                   { 399.808, 648.143, 834.221, 1067.553, 1143.569, 1700.0 } },
        { 10000.0, { 399.779, 639.088, 805.088, 994.974, 1052.381, 1700.0 },
                   { 399.717, 628.661, 781.734, 956.502, 1009.985, 1700.0 } },
    };
    for (const Row& row : rows) {
        double src = (std::max)(PQ(row.peak), tgt);
        for (int i = 0; i < 6; i++) {
            double cNits = content[i] < 0.0 ? row.peak : content[i];
            INFO("frame peak " << row.peak << "  content " << cNits << " nits");
            CHECK(Nits(ApplyIChannel(NewSoftClip, PQ(cNits), src, tgt, tgtNits)) == doctest::Approx(row.softClip[i]).epsilon(2e-5));
            CHECK(Nits(ApplyIChannel(NewReinhard, PQ(cNits), src, tgt, tgtNits)) == doctest::Approx(row.reinhard[i]).epsilon(2e-5));
        }
    }

    // The defect this replaces (ef0f703), and the original it restores, for the record:
    // 4000-nit frame: its peak showed at 985 (ef0f703) / 1250 (pre-March); 1000-nit content at 633 / 778
    double src4k = PQ(4000.0);
    CHECK(Nits(ApplyIChannel(Ef0f703SoftClip, src4k, src4k, tgt, tgtNits)) == doctest::Approx(984.61).epsilon(1e-4));
    CHECK(Nits(ApplyIChannel(PreMarchSoftClip, src4k, src4k, tgt, tgtNits)) == doctest::Approx(1249.65).epsilon(1e-4));
    CHECK(Nits(ApplyIChannel(Ef0f703SoftClip, PQ(1000.0), src4k, tgt, tgtNits)) == doctest::Approx(632.82).epsilon(1e-4));
    CHECK(Nits(ApplyIChannel(PreMarchSoftClip, PQ(1000.0), src4k, tgt, tgtNits)) == doctest::Approx(778.33).epsilon(1e-4));
    // ef0f703's knee kink: slope H/S
    double k = 0.8 * tgt;
    CHECK((tgt - k) / (src4k - k) == doctest::Approx(0.6356).epsilon(1e-3));
}

// =============================================================================================
// The real HLSL on WARP
// =============================================================================================

TEST_CASE("Tonemap curves: the shared HLSL on WARP matches the CPU port") {
    CurvesGpu gpu;
    if (!gpu.Init()) {
        if (gpu.deviceUnavailable) { MESSAGE("skipping GPU tonemap-curve test: " << gpu.error); return; }
        FAIL("tonemap-curve GPU harness init failed: " << gpu.error);
    }

    // Per case: a sweep of I over [0, 1.05 * src], then src itself, then the two knee-slope probes
    const int kSweep = 512;
    std::vector<Case> cases = CaseGrid();
    std::vector<float> packed;
    for (const Case& c : cases) {
        float tgt = (float)PQ(c.tgtNits), src = (float)PQ(c.srcNits);
        float k = c.tgtNits <= 203.0 ? 0.0f : tgt * 0.8f;
        float h = 1e-2f * (src - k);
        auto push = [&](float I) { packed.insert(packed.end(), { I, src, tgt, (float)c.tgtNits }); };
        for (int i = 0; i <= kSweep; i++) push((std::min)(1.05f * src, 1.0f) * i / kSweep);
        push(src);
        push(k + h);
        push(k + 0.5f * h);
    }
    auto out = gpu.Run(packed);
    REQUIRE(out.size() == packed.size() / 4);

    const int perCase = kSweep + 4;
    for (size_t ci = 0; ci < cases.size(); ci++) {
        const Case& c = cases[ci];
        INFO("target " << c.tgtNits << "  source peak " << c.srcNits);
        const float* in = &packed[ci * perCase * 4];
        const auto* res = &out[ci * perCase];
        float src = in[1], tgt = in[2];

        double worstSC = 0.0, worstRH = 0.0;
        bool monotone = true, belowTarget = true;
        for (int i = 0; i <= kSweep; i++) {
            double I = in[i * 4], s = in[i * 4 + 1], t = in[i * 4 + 2], n = in[i * 4 + 3];
            worstSC = (std::max)(worstSC, std::fabs(res[i].first - NewSoftClip(I, s, t, n)));
            worstRH = (std::max)(worstRH, std::fabs(res[i].second - NewReinhard(I, s, t, n)));
            if (res[i].first > tgt || res[i].second > tgt) belowTarget = false;
            if (i > 0 && (res[i].first < res[i - 1].first - 1e-6f || res[i].second < res[i - 1].second - 1e-6f))
                monotone = false;
        }
        CHECK(worstSC < 2e-6);
        CHECK(worstRH < 2e-6);
        CHECK(monotone);
        CHECK(belowTarget);

        // source peak -> target (spec: within 1e-5 PQ)
        CHECK(std::fabs(res[kSweep + 1].first - tgt) < 1e-5);
        CHECK(std::fabs(res[kSweep + 1].second - tgt) < 1e-5);

        // knee slope 1, Richardson on the two probes (the HLSL's knee: pqTgtPeak * 0.8 in float)
        double k = c.tgtNits <= 203.0 ? 0.0 : (double)(tgt * 0.8f);
        double h = (double)in[(kSweep + 2) * 4] - k, h2 = (double)in[(kSweep + 3) * 4] - k;
        for (int which = 0; which < 2; which++) {
            double v1 = which ? res[kSweep + 2].second : res[kSweep + 2].first;
            double v2 = which ? res[kSweep + 3].second : res[kSweep + 3].first;
            double slope = 2.0 * (v2 - k) / h2 - (v1 - k) / h;
            INFO((which ? "Reinhard" : "SoftClip") << " knee slope " << slope);
            CHECK(std::fabs(slope - 1.0) < 2e-3);
        }
    }
}

// =============================================================================================
// Both production pixel shaders carry the shared text and compile
// =============================================================================================

TEST_CASE("Tonemap curves: both production pixel shaders splice the shared text and compile") {
    const std::string shared = DLUT_TONEMAP_CURVES_HLSL;
    const std::string overlay = g_psSource;
    const std::string hook(g_shaders);
    CHECK(overlay.find(shared) != std::string::npos);
    CHECK(hook.find(shared) != std::string::npos);
    // exactly one definition each (no stale local copy left behind)
    for (const std::string* src : { &overlay, &hook }) {
        size_t first = src->find("float TonemapSoftClip_PQ(");
        CHECK(first != std::string::npos);
        CHECK(src->find("float TonemapSoftClip_PQ(", first + 1) == std::string::npos);
        first = src->find("float TonemapReinhard_PQ(");
        CHECK(first != std::string::npos);
        CHECK(src->find("float TonemapReinhard_PQ(", first + 1) == std::string::npos);
    }

    // Compiled exactly as production does: gpu.cpp (strlen, "main") and hook_render.cpp (sizeof, "PS")
    auto compile = [](const char* text, size_t len, const char* entry, std::string& output) {
        ID3DBlob* blob = nullptr;
        ID3DBlob* msgs = nullptr;
        HRESULT hr = D3DCompile(text, len, nullptr, nullptr, nullptr, entry, "ps_5_0", 0, 0, &blob, &msgs);
        if (msgs) { output = (const char*)msgs->GetBufferPointer(); msgs->Release(); }
        if (blob) blob->Release();
        return SUCCEEDED(hr);
    };
    std::string overlayMsgs, hookMsgs;
    bool overlayOk = compile(g_psSource, strlen(g_psSource), "main", overlayMsgs);
    bool hookOk = compile(g_shaders, sizeof g_shaders, "PS", hookMsgs);
    INFO("overlay compiler output: " << overlayMsgs);
    INFO("hook compiler output: " << hookMsgs);
    CHECK(overlayOk);
    CHECK(hookOk);
}
