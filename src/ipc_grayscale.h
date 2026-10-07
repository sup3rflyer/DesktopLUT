// DesktopLUT - ipc_grayscale.h
// Calibration-pipe grayscale payload -> GrayscaleSettings, validated BEFORE anything is allocated or
// stored (header-only and pure so tests reach it). Used by mhc.set_base_grayscale,
// mhc.set_correction_grayscale, mhc.grayscale_set_live and runtime.set_grayscale_tweak.
//
// Payload: { point_count?, points?, deviations?{r,g,b}, luminance?, rgb?{r,g,b} }
//   point_count   10, 20 or 32. Absent: the length of `points`, else the slot's current count.
//   points        exactly point_count output levels in [0, 2] (above 1 saturates in the bake; see
//                 kMaxGrayscalePointLevel). Absent: the slot's identity curve
//                 for the mode (SDR t^2 grid / HDR uniform PQ) — never a uniform ramp, which on the
//                 SDR grid is a square-root curve.
//   deviations    composed per-channel gains (back-compat form), each exactly point_count in [0, 8].
//   luminance     the editor's main slider per point (scales points), exactly point_count in [0, 8].
//   rgb           the editor's per-channel balance strips, each exactly point_count in [0, 8]; with
//                 luminance but without rgb, the balance is recovered from deviations — only when
//                 deviations were sent (else 1: dividing default gains by luminance cancelled it).
// points x luminance must stay within [0, 2] (float noise of 1e-4 below 0 is clamped); anything else
// is an error naming the field, and the slot is left untouched.

#pragma once

#include "grayscale_validate.h"
#include "ipc_json.h"

#include <algorithm>
#include <cmath>
#include <string>
#include <vector>

namespace ipc_grayscale {

// Reads an optional numeric array that must have exactly n finite elements in [lo, hi].
// Returns false (with error) when present but malformed; `present` reports whether it was sent.
inline bool ReadExactArray(const ipc_json::JsonValue* v, int n, float lo, float hi, const char* name,
                           std::vector<float>& out, bool& present, std::string& error) {
    present = false;
    out.clear();
    if (!v || v->type == ipc_json::JsonValue::Null) return true;
    if (v->type != ipc_json::JsonValue::Arr) { error = std::string(name) + " must be an array"; return false; }
    if ((int)v->arr.size() != n) {
        error = std::string(name) + " must have exactly point_count (" + std::to_string(n) + ") values, got " +
                std::to_string(v->arr.size());
        return false;
    }
    out.reserve(n);
    for (const auto& e : v->arr) {
        if (e.type != ipc_json::JsonValue::Num || !std::isfinite(e.num) || e.num < lo || e.num > hi) {
            error = std::string(name) + " values must be finite numbers in [" + std::to_string(lo) + ", " +
                    std::to_string(hi) + "]";
            return false;
        }
        out.push_back((float)e.num);
    }
    present = true;
    return true;
}

inline bool GrayscaleFromPayload(const ipc_json::JsonValue& p, bool isHDR, int currentPointCount,
                                 GrayscaleSettings& out, std::string& error) {
    using ipc_json::JsonValue;
    // Point count first, so nothing below allocates by an attacker-chosen size.
    int pc = 0;
    const JsonValue* pcv = p.find("point_count");
    const JsonValue* ptsv = p.find("points");
    if (pcv && pcv->type != JsonValue::Null) {
        if (pcv->type != JsonValue::Num || !std::isfinite(pcv->num) || pcv->num != std::floor(pcv->num)) {
            error = "point_count must be an integer";
            return false;
        }
        pc = (pcv->num >= 0.0 && pcv->num <= 1000.0) ? (int)pcv->num : -1;
    } else if (ptsv && ptsv->type == JsonValue::Arr) {
        pc = (int)(std::min)(ptsv->arr.size(), (size_t)1000);
    } else {
        pc = currentPointCount;
    }
    if (!IsValidGrayscalePointCount(pc)) {
        error = "point_count must be 10, 20 or 32";
        return false;
    }

    // A slightly loose lower bound so float noise below 0 from the client is clamped, not rejected.
    constexpr float kEps = 1e-4f;
    std::vector<float> pts, lum, devR, devG, devB, balR, balG, balB;
    bool havePts = false, haveLum = false, hdR = false, hdG = false, hdB = false, hbR = false, hbG = false, hbB = false;
    const JsonValue* dev = p.find("deviations");
    const JsonValue* rgb = p.find("rgb");
    if (dev && dev->type != JsonValue::Null && dev->type != JsonValue::Obj) { error = "deviations must be an object"; return false; }
    if (rgb && rgb->type != JsonValue::Null && rgb->type != JsonValue::Obj) { error = "rgb must be an object"; return false; }
    const bool devObj = dev && dev->type == JsonValue::Obj;
    const bool rgbObj = rgb && rgb->type == JsonValue::Obj;
    if (!ReadExactArray(ptsv, pc, -kEps, kMaxGrayscalePointLevel, "points", pts, havePts, error)) return false;
    if (!ReadExactArray(p.find("luminance"), pc, 0.0f, 8.0f, "luminance", lum, haveLum, error)) return false;
    if (!ReadExactArray(devObj ? dev->find("r") : nullptr, pc, 0.0f, 8.0f, "deviations.r", devR, hdR, error)) return false;
    if (!ReadExactArray(devObj ? dev->find("g") : nullptr, pc, 0.0f, 8.0f, "deviations.g", devG, hdG, error)) return false;
    if (!ReadExactArray(devObj ? dev->find("b") : nullptr, pc, 0.0f, 8.0f, "deviations.b", devB, hdB, error)) return false;
    if (!ReadExactArray(rgbObj ? rgb->find("r") : nullptr, pc, 0.0f, 8.0f, "rgb.r", balR, hbR, error)) return false;
    if (!ReadExactArray(rgbObj ? rgb->find("g") : nullptr, pc, 0.0f, 8.0f, "rgb.g", balG, hbG, error)) return false;
    if (!ReadExactArray(rgbObj ? rgb->find("b") : nullptr, pc, 0.0f, 8.0f, "rgb.b", balB, hbB, error)) return false;
    if ((hdR || hdG || hdB) && !(hdR && hdG && hdB)) { error = "deviations needs r, g and b"; return false; }
    if ((hbR || hbG || hbB) && !(hbR && hbG && hbB)) { error = "rgb needs r, g and b"; return false; }
    const bool haveDev = hdR;
    const bool haveRgb = hbR;

    GrayscaleSettings gs;
    gs.pointCount = pc;
    if (havePts) gs.points = pts;
    else InitIdentityGrayscale(gs, isHDR);   // also resets the gains, overwritten below
    if (haveLum) {
        for (int k = 0; k < pc; ++k) gs.points[k] *= lum[k];
    }
    for (int k = 0; k < pc; ++k) {
        if (gs.points[k] > kMaxGrayscalePointLevel) {
            error = "points x luminance must stay within [0, 2] (point " + std::to_string(k) + ")";
            return false;
        }
        gs.points[k] = (std::max)(0.0f, gs.points[k]);
    }

    // Per-channel gains: the decomposition (rgb) when sent; else, with luminance, the balance
    // recovered from the composed deviations (deviations = luminance x rgb); else the deviations.
    std::vector<float>* src[3] = { nullptr, nullptr, nullptr };
    std::vector<float> recR, recG, recB;
    if (haveRgb) {
        src[0] = &balR; src[1] = &balG; src[2] = &balB;
    } else if (haveLum && haveDev) {
        recR.assign(pc, 1.0f); recG.assign(pc, 1.0f); recB.assign(pc, 1.0f);
        for (int k = 0; k < pc; ++k) {
            const float l = lum[k];
            if (std::fabs(l) > 1e-6f) { recR[k] = devR[k] / l; recG[k] = devG[k] / l; recB[k] = devB[k] / l; }
        }
        src[0] = &recR; src[1] = &recG; src[2] = &recB;
    } else if (haveDev && !haveLum) {
        src[0] = &devR; src[1] = &devG; src[2] = &devB;
    }
    for (int ch = 0; ch < 3; ++ch) {
        if (src[ch]) gs.rgbDeviations[ch] = *src[ch];
        else gs.rgbDeviations[ch].assign(pc, 1.0f);
        if (!GrayscaleGainsValid(gs.rgbDeviations[ch], pc)) {
            error = "recovered per-channel balance out of range (deviations / luminance)";
            return false;
        }
    }

    out.pointCount = gs.pointCount;
    out.points = std::move(gs.points);
    for (int ch = 0; ch < 3; ++ch) out.rgbDeviations[ch] = std::move(gs.rgbDeviations[ch]);
    out.enabled = true;
    return true;
}

}  // namespace ipc_grayscale
