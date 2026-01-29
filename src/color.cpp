// DesktopLUT - color.cpp
// Color space mathematics and primaries calculations

#include "color.h"
#include <cmath>
#include <iostream>

// Calculate 3x3 color matrix from source primaries to target primaries
// Uses Bradford chromatic adaptation for white point conversion
// Reference: Bruce Lindbloom (brucelindbloom.com)
void CalculatePrimariesMatrix(const DisplayPrimariesData& src, const DisplayPrimariesData& target, float outMatrix[9]) {
    // Helper: multiply 3x3 matrices (row-major)
    auto matMul = [](const float a[9], const float b[9], float out[9]) {
        for (int i = 0; i < 3; i++) {
            for (int j = 0; j < 3; j++) {
                out[i * 3 + j] = a[i * 3 + 0] * b[0 * 3 + j] +
                                 a[i * 3 + 1] * b[1 * 3 + j] +
                                 a[i * 3 + 2] * b[2 * 3 + j];
            }
        }
    };

    // Helper: invert 3x3 matrix
    auto matInv = [](const float m[9], float out[9]) -> bool {
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
    };

    // Convert chromaticity (x, y) to XYZ (assuming Y=1)
    auto xyToXYZ = [](float x, float y, float* X, float* Y, float* Z) {
        if (y < 1e-6f) y = 1e-6f;
        *X = x / y;
        *Y = 1.0f;
        *Z = (1.0f - x - y) / y;
    };

    // Bradford matrix (D50 reference)
    const float Ma[9] = {
         0.8951f,  0.2664f, -0.1614f,
        -0.7502f,  1.7135f,  0.0367f,
         0.0389f, -0.0685f,  1.0296f
    };
    float MaInv[9];
    if (!matInv(Ma, MaInv)) {
        // Bradford matrix is constant and always invertible, but check anyway
        std::cerr << "Error: Bradford matrix inversion failed" << std::endl;
        // Return identity matrix
        for (int i = 0; i < 9; i++) outMatrix[i] = (i % 4 == 0) ? 1.0f : 0.0f;
        return;
    }

    // Get XYZ of white points
    float srcWX, srcWY, srcWZ;
    float tgtWX, tgtWY, tgtWZ;
    xyToXYZ(src.Wx, src.Wy, &srcWX, &srcWY, &srcWZ);
    xyToXYZ(target.Wx, target.Wy, &tgtWX, &tgtWY, &tgtWZ);

    // Calculate Bradford cone response domain coordinates
    float srcCone[3] = {
        Ma[0] * srcWX + Ma[1] * srcWY + Ma[2] * srcWZ,
        Ma[3] * srcWX + Ma[4] * srcWY + Ma[5] * srcWZ,
        Ma[6] * srcWX + Ma[7] * srcWY + Ma[8] * srcWZ
    };
    float tgtCone[3] = {
        Ma[0] * tgtWX + Ma[1] * tgtWY + Ma[2] * tgtWZ,
        Ma[3] * tgtWX + Ma[4] * tgtWY + Ma[5] * tgtWZ,
        Ma[6] * tgtWX + Ma[7] * tgtWY + Ma[8] * tgtWZ
    };

    // Chromatic adaptation diagonal matrix
    float scale[9] = {
        tgtCone[0] / srcCone[0], 0, 0,
        0, tgtCone[1] / srcCone[1], 0,
        0, 0, tgtCone[2] / srcCone[2]
    };

    // Check if white points are similar enough to skip adaptation
    // (for colorimetric accuracy, skip Bradford when white points match)
    bool skipAdaptation = (fabs(src.Wx - target.Wx) < 0.01f && fabs(src.Wy - target.Wy) < 0.01f);

    float adapt[9];
    if (skipAdaptation) {
        // Identity adaptation - pure colorimetric mapping (white points similar)
        adapt[0] = 1; adapt[1] = 0; adapt[2] = 0;
        adapt[3] = 0; adapt[4] = 1; adapt[5] = 0;
        adapt[6] = 0; adapt[7] = 0; adapt[8] = 1;
    } else {
        // Build chromatic adaptation matrix: MaInv * scale * Ma
        float tmp[9];
        matMul(scale, Ma, tmp);
        matMul(MaInv, tmp, adapt);
    }

    // Build RGB to XYZ matrix for source primaries
    float srcRX, srcRY, srcRZ, srcGX, srcGY, srcGZ, srcBX, srcBY, srcBZ;
    xyToXYZ(src.Rx, src.Ry, &srcRX, &srcRY, &srcRZ);
    xyToXYZ(src.Gx, src.Gy, &srcGX, &srcGY, &srcGZ);
    xyToXYZ(src.Bx, src.By, &srcBX, &srcBY, &srcBZ);

    // Primaries matrix (columns are R, G, B XYZ)
    float srcPrim[9] = {
        srcRX, srcGX, srcBX,
        srcRY, srcGY, srcBY,
        srcRZ, srcGZ, srcBZ
    };
    float srcPrimInv[9];
    if (!matInv(srcPrim, srcPrimInv)) {
        std::cerr << "Error: Source primaries matrix is singular (degenerate primaries)" << std::endl;
        // Return identity matrix
        for (int i = 0; i < 9; i++) outMatrix[i] = (i % 4 == 0) ? 1.0f : 0.0f;
        return;
    }

    // Solve for S (luminance scaling) such that XYZ_white = srcPrim * S
    float srcS[3] = {
        srcPrimInv[0] * srcWX + srcPrimInv[1] * srcWY + srcPrimInv[2] * srcWZ,
        srcPrimInv[3] * srcWX + srcPrimInv[4] * srcWY + srcPrimInv[5] * srcWZ,
        srcPrimInv[6] * srcWX + srcPrimInv[7] * srcWY + srcPrimInv[8] * srcWZ
    };

    // Source RGB to XYZ matrix
    float srcRGBtoXYZ[9] = {
        srcRX * srcS[0], srcGX * srcS[1], srcBX * srcS[2],
        srcRY * srcS[0], srcGY * srcS[1], srcBY * srcS[2],
        srcRZ * srcS[0], srcGZ * srcS[1], srcBZ * srcS[2]
    };

    // Build RGB to XYZ matrix for target primaries
    float tgtRX, tgtRY, tgtRZ, tgtGX, tgtGY, tgtGZ, tgtBX, tgtBY, tgtBZ;
    xyToXYZ(target.Rx, target.Ry, &tgtRX, &tgtRY, &tgtRZ);
    xyToXYZ(target.Gx, target.Gy, &tgtGX, &tgtGY, &tgtGZ);
    xyToXYZ(target.Bx, target.By, &tgtBX, &tgtBY, &tgtBZ);

    float tgtPrim[9] = {
        tgtRX, tgtGX, tgtBX,
        tgtRY, tgtGY, tgtBY,
        tgtRZ, tgtGZ, tgtBZ
    };
    float tgtPrimInv[9];
    matInv(tgtPrim, tgtPrimInv);

    float tgtS[3] = {
        tgtPrimInv[0] * tgtWX + tgtPrimInv[1] * tgtWY + tgtPrimInv[2] * tgtWZ,
        tgtPrimInv[3] * tgtWX + tgtPrimInv[4] * tgtWY + tgtPrimInv[5] * tgtWZ,
        tgtPrimInv[6] * tgtWX + tgtPrimInv[7] * tgtWY + tgtPrimInv[8] * tgtWZ
    };

    float tgtRGBtoXYZ[9] = {
        tgtRX * tgtS[0], tgtGX * tgtS[1], tgtBX * tgtS[2],
        tgtRY * tgtS[0], tgtGY * tgtS[1], tgtBY * tgtS[2],
        tgtRZ * tgtS[0], tgtGZ * tgtS[1], tgtBZ * tgtS[2]
    };
    float tgtXYZtoRGB[9];
    if (!matInv(tgtRGBtoXYZ, tgtXYZtoRGB)) {
        std::cerr << "Error: Target primaries matrix is singular (degenerate primaries)" << std::endl;
        // Return identity matrix
        for (int i = 0; i < 9; i++) outMatrix[i] = (i % 4 == 0) ? 1.0f : 0.0f;
        return;
    }

    // Final matrix: tgtXYZtoRGB * adapt * srcRGBtoXYZ
    float tmp2[9];
    matMul(adapt, srcRGBtoXYZ, tmp2);
    matMul(tgtXYZtoRGB, tmp2, outMatrix);

    // Debug output - identify source by red primary (sRGB=0.64, Rec.2020=0.708)
    const char* srcName = (src.Rx > 0.68f) ? "Rec.2020" : "sRGB";
    std::cout << "Primaries matrix: " << srcName << " -> display ("
              << target.Rx << "," << target.Ry << " / "
              << target.Gx << "," << target.Gy << " / "
              << target.Bx << "," << target.By << ")" << std::endl;
    std::cout << "  [" << outMatrix[0] << ", " << outMatrix[1] << ", " << outMatrix[2] << "]" << std::endl;
    std::cout << "  [" << outMatrix[3] << ", " << outMatrix[4] << ", " << outMatrix[5] << "]" << std::endl;
    std::cout << "  [" << outMatrix[6] << ", " << outMatrix[7] << ", " << outMatrix[8] << "]" << std::endl;
}
