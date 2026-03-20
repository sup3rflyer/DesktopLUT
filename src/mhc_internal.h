// DesktopLUT - mhc_internal.h
// Shared inline helpers for MHC ICC binary format handling
// Used by both mhc_icc.cpp (writing) and mhc_read.cpp (reading)

#pragma once

#include <cstdint>
#include <cmath>

// Make a 4-byte ICC signature from string
inline uint32_t MakeSig(const char s[4]) {
    return ((uint32_t)(uint8_t)s[0] << 24) |
           ((uint32_t)(uint8_t)s[1] << 16) |
           ((uint32_t)(uint8_t)s[2] << 8) |
           ((uint32_t)(uint8_t)s[3]);
}

// 3x3 matrix inverse (returns false if singular)
inline bool MatInv3(const float m[9], float out[9]) {
    float det = m[0] * (m[4] * m[8] - m[5] * m[7]) -
                m[1] * (m[3] * m[8] - m[5] * m[6]) +
                m[2] * (m[3] * m[7] - m[4] * m[6]);
    if (fabs(det) < 1e-10f) return false;
    float invDet = 1.0f / det;
    out[0] = (m[4] * m[8] - m[5] * m[7]) * invDet;
    out[1] = (m[2] * m[7] - m[1] * m[8]) * invDet;
    out[2] = (m[1] * m[5] - m[2] * m[4]) * invDet;
    out[3] = (m[5] * m[6] - m[3] * m[8]) * invDet;
    out[4] = (m[0] * m[8] - m[2] * m[6]) * invDet;
    out[5] = (m[2] * m[3] - m[0] * m[5]) * invDet;
    out[6] = (m[3] * m[7] - m[4] * m[6]) * invDet;
    out[7] = (m[1] * m[6] - m[0] * m[7]) * invDet;
    out[8] = (m[0] * m[4] - m[1] * m[3]) * invDet;
    return true;
}

// 3x3 matrix * 3-vector multiply
inline void MatVecMul3(const float m[9], const float v[3], float out[3]) {
    out[0] = m[0] * v[0] + m[1] * v[1] + m[2] * v[2];
    out[1] = m[3] * v[0] + m[4] * v[1] + m[5] * v[2];
    out[2] = m[6] * v[0] + m[7] * v[1] + m[8] * v[2];
}
