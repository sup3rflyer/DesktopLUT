// DesktopLUT - mhc_read.cpp
// ICC profile reading, grayscale extraction, and 1D cube loading

#include "mhc.h"
#include "mhc_internal.h"
#include <iostream>
#include <fstream>
#include <sstream>
#include <locale>
#include <cstring>
#include <cmath>
#include <algorithm>

// ============================================================================
// SECTION: ICC Profile Reading
// ============================================================================

// Read big-endian 32-bit from buffer
uint32_t ReadBE32(const uint8_t* p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

// Read big-endian 16-bit from buffer
uint16_t ReadBE16(const uint8_t* p) {
    return ((uint16_t)p[0] << 8) | p[1];
}

// Read s15Fixed16Number
float ReadS15Fixed16(const uint8_t* p) {
    int32_t val = (int32_t)ReadBE32(p);
    return (float)val / 65536.0f;
}

// Bradford D50->D65 matrix (inverse of D65->D50, ICC spec / Bruce Lindbloom)
static const float g_bradfordD50toD65[9] = {
     0.9555766f, -0.0230393f,  0.0631636f,
    -0.0282895f,  1.0099416f,  0.0210077f,
     0.0122982f, -0.0204830f,  1.3299098f
};

bool ReadICCProfile(const std::wstring& path, ICCProfileData& outData) {
    outData = {};

    // Read file
    std::ifstream file(path, std::ios::binary);
    if (!file.is_open()) return false;
    file.seekg(0, std::ios::end);
    size_t fileSize = (size_t)file.tellg();
    if (fileSize < 132) return false;  // Minimum: 128 header + 4 tag count
    file.seekg(0);
    std::vector<uint8_t> data(fileSize);
    file.read(reinterpret_cast<char*>(data.data()), fileSize);
    file.close();

    const uint8_t* d = data.data();

    // Verify ICC signature at offset 36
    if (ReadBE32(d + 36) != MakeSig("acsp")) {
        std::cerr << "ICC: Invalid signature (not 'acsp')" << std::endl;
        return false;
    }

    // Read tag table
    uint32_t tagCount = ReadBE32(d + 128);
    if (tagCount > 200) return false;  // Sanity: no real ICC has >~30 tags
    if (128 + 4 + (uint64_t)tagCount * 12 > fileSize) return false;

    struct TagInfo { uint32_t sig, offset, size; };
    std::vector<TagInfo> tags(tagCount);
    for (uint32_t i = 0; i < tagCount; i++) {
        size_t base = 132 + i * 12;
        tags[i].sig = ReadBE32(d + base);
        tags[i].offset = ReadBE32(d + base + 4);
        tags[i].size = ReadBE32(d + base + 8);
    }

    auto findTag = [&](uint32_t sig) -> const TagInfo* {
        for (auto& t : tags) if (t.sig == sig) return &t;
        return nullptr;
    };

    // Read chromatic adaptation matrix (chad tag) for un-adapting primaries
    float chadInv[9] = { 1,0,0, 0,1,0, 0,0,1 };
    bool hasChadInv = false;
    if (auto* tag = findTag(MakeSig("chad"))) {
        if (tag->offset + 44 <= fileSize) {
            const uint8_t* p = d + tag->offset;
            if (ReadBE32(p) == MakeSig("sf32")) {
                float chad[9];
                for (int i = 0; i < 9; i++)
                    chad[i] = ReadS15Fixed16(p + 8 + i * 4);
                hasChadInv = MatInv3(chad, chadInv);
            }
        }
    }
    if (!hasChadInv) {
        // Use standard D50->D65 Bradford as fallback
        memcpy(chadInv, g_bradfordD50toD65, sizeof(chadInv));
    }

    // Read rXYZ/gXYZ/bXYZ tags -> extract primaries
    auto readXYZTag = [&](uint32_t sig, float outXYZ[3]) -> bool {
        auto* tag = findTag(sig);
        if (!tag || tag->offset + 20 > fileSize) return false;
        const uint8_t* p = d + tag->offset;
        if (ReadBE32(p) != MakeSig("XYZ ")) return false;
        outXYZ[0] = ReadS15Fixed16(p + 8);
        outXYZ[1] = ReadS15Fixed16(p + 12);
        outXYZ[2] = ReadS15Fixed16(p + 16);
        return true;
    };

    float rXYZ[3], gXYZ[3], bXYZ[3];
    if (readXYZTag(MakeSig("rXYZ"), rXYZ) &&
        readXYZTag(MakeSig("gXYZ"), gXYZ) &&
        readXYZTag(MakeSig("bXYZ"), bXYZ)) {
        // Un-adapt from D50 PCS to D65
        float rD65[3], gD65[3], bD65[3];
        MatVecMul3(chadInv, rXYZ, rD65);
        MatVecMul3(chadInv, gXYZ, gD65);
        MatVecMul3(chadInv, bXYZ, bD65);

        // Convert XYZ to CIE xy
        auto xyzToXy = [](const float xyz[3], float& x, float& y) {
            float sum = xyz[0] + xyz[1] + xyz[2];
            if (sum < 1e-6f) { x = 0.3127f; y = 0.3290f; return; }
            x = xyz[0] / sum;
            y = xyz[1] / sum;
        };
        xyzToXy(rD65, outData.primaries.Rx, outData.primaries.Ry);
        xyzToXy(gD65, outData.primaries.Gx, outData.primaries.Gy);
        xyzToXy(bD65, outData.primaries.Bx, outData.primaries.By);
        // White point from sum of un-adapted primaries (native display white)
        float wXYZ[3] = { rD65[0]+gD65[0]+bD65[0], rD65[1]+gD65[1]+bD65[1], rD65[2]+gD65[2]+bD65[2] };
        xyzToXy(wXYZ, outData.primaries.Wx, outData.primaries.Wy);
        outData.hasPrimaries = true;
    }

    // Read chrm tag for precise primaries (no chromatic un-adaptation needed)
    // chrm contains measured CIE xy chromaticities directly, so it's more accurate
    // than un-adapting rXYZ/gXYZ/bXYZ when the profile uses a non-standard CAT
    // (e.g., DaVinci Resolve profiles have 'arts' tag instead of 'chad')
    if (auto* tag = findTag(MakeSig("chrm"))) {
        if (tag->offset + 36 <= fileSize) {  // 12 header + 3*8 data minimum
            const uint8_t* p = d + tag->offset;
            uint16_t numChannels = ReadBE16(p + 8);
            if (numChannels >= 3) {
                float cRx = ReadS15Fixed16(p + 12);
                float cRy = ReadS15Fixed16(p + 16);
                float cGx = ReadS15Fixed16(p + 20);
                float cGy = ReadS15Fixed16(p + 24);
                float cBx = ReadS15Fixed16(p + 28);
                float cBy = ReadS15Fixed16(p + 32);
                // Sanity: all values should be in valid chromaticity range
                if (cRx > 0.0f && cRx < 1.0f && cRy > 0.0f && cRy < 1.0f &&
                    cGx > 0.0f && cGx < 1.0f && cGy > 0.0f && cGy < 1.0f &&
                    cBx > 0.0f && cBx < 1.0f && cBy > 0.0f && cBy < 1.0f) {
                    outData.primaries.Rx = cRx; outData.primaries.Ry = cRy;
                    outData.primaries.Gx = cGx; outData.primaries.Gy = cGy;
                    outData.primaries.Bx = cBx; outData.primaries.By = cBy;
                    outData.hasPrimaries = true;
                    // White point: keep from rXYZ sum above, or default to D65
                    if (outData.primaries.Wx == 0.0f && outData.primaries.Wy == 0.0f) {
                        outData.primaries.Wx = 0.3127f;
                        outData.primaries.Wy = 0.3290f;
                    }
                    std::cout << "ICC: Using chrm tag for primaries (bypasses CAT un-adaptation)" << std::endl;
                }
            }
        }
    }

    // Detect LUT-based profiles (A2B0 or B2A0 tags present)
    // LUT-based profiles have approximate fallback TRCs that are unsuitable for 1D correction
    if (findTag(MakeSig("A2B0")) || findTag(MakeSig("B2A0")))
        outData.isLUTBased = true;

    // Read rTRC/gTRC/bTRC tags -> extract transfer curves
    auto readCurvTag = [&](uint32_t sig, std::vector<float>& outCurve, float& outGamma) -> bool {
        auto* tag = findTag(sig);
        if (!tag || tag->offset + 12 > fileSize) return false;
        const uint8_t* p = d + tag->offset;
        uint32_t typeSig = ReadBE32(p);

        if (typeSig == MakeSig("curv")) {
            uint32_t count = ReadBE32(p + 8);
            if (count == 0) {
                // Identity (gamma 1.0)
                outGamma = 1.0f;
                outCurve.resize(256);
                for (int i = 0; i < 256; i++) outCurve[i] = (float)i / 255.0f;
                return true;
            } else if (count == 1) {
                // Parametric gamma (u8Fixed8)
                if (tag->offset + 14 > fileSize) return false;
                outGamma = (float)ReadBE16(p + 12) / 256.0f;
                outCurve.resize(256);
                for (int i = 0; i < 256; i++) {
                    float t = (float)i / 255.0f;
                    outCurve[i] = powf(t, outGamma);
                }
                return true;
            } else {
                // Tabular data
                if (count > 65536) return false;  // Sanity: reasonable max for tabular TRC
                if ((uint64_t)tag->offset + 12 + (uint64_t)count * 2 > fileSize) return false;
                outCurve.resize(count);
                for (uint32_t i = 0; i < count; i++) {
                    outCurve[i] = (float)ReadBE16(p + 12 + i * 2) / 65535.0f;
                }
                return true;
            }
        } else if (typeSig == MakeSig("para")) {
            // Parametric curve type
            if (tag->offset + 16 > fileSize) return false;
            uint16_t funcType = ReadBE16(p + 8);
            if (funcType == 0) {
                // Type 0: Y = X^g
                outGamma = ReadS15Fixed16(p + 12);
                outCurve.resize(256);
                for (int i = 0; i < 256; i++) {
                    float t = (float)i / 255.0f;
                    outCurve[i] = powf(t, outGamma);
                }
                return true;
            }
            // Other para types (1-4) are more complex; generate curve from the function
            // For simplicity, handle type 3 (sRGB-like: Y = (aX+b)^g + c for X>=d, else eX+f)
            if (funcType == 3 && tag->offset + 12 + 7 * 4 <= fileSize) {
                float g = ReadS15Fixed16(p + 12);
                float a = ReadS15Fixed16(p + 16);
                float b = ReadS15Fixed16(p + 20);
                float c = ReadS15Fixed16(p + 24);
                float d_param = ReadS15Fixed16(p + 28);
                float e = ReadS15Fixed16(p + 32);
                float f = ReadS15Fixed16(p + 36);
                outCurve.resize(256);
                for (int i = 0; i < 256; i++) {
                    float x = (float)i / 255.0f;
                    if (x >= d_param) {
                        outCurve[i] = powf(a * x + b, g) + c;
                    } else {
                        outCurve[i] = e * x + f;
                    }
                    outCurve[i] = std::clamp(outCurve[i], 0.0f, 1.0f);
                }
                return true;
            }
            // Unsupported para types 1, 2, 4 — return false so caller sees hasTRC=false
            return false;
        }
        return false;
    };

    float gammaR = 0, gammaG = 0, gammaB = 0;
    bool hasR = readCurvTag(MakeSig("rTRC"), outData.trcR, gammaR);
    bool hasG = readCurvTag(MakeSig("gTRC"), outData.trcG, gammaG);
    bool hasB = readCurvTag(MakeSig("bTRC"), outData.trcB, gammaB);
    if (hasR && hasG && hasB) {
        outData.hasTRC = true;
        // If all are single gamma, record average
        if (gammaR > 0 && gammaG > 0 && gammaB > 0) {
            outData.gamma = (gammaR + gammaG + gammaB) / 3.0f;
            outData.hasGamma = true;
        }

        // Normalize each TRC channel to reach 1.0 at full signal.
        // In Curves+Matrix profiles (especially HDR), per-channel TRC maxes can differ
        // (e.g. bTRC[max]=0.72 on wide-gamut displays). Without normalization, the 1D LUT
        // correction compensates for the absolute channel imbalance (boosting blue) — but
        // that's the matrix's job. The 1D LUT should only correct the transfer function SHAPE
        // (PQ tracking / gamma linearity). Normalizing ensures clean separation.
        auto normalizeTRC = [](std::vector<float>& trc) {
            if (trc.empty()) return;
            float trcMax = trc.back();
            if (trcMax > 0.0f && trcMax < 0.999f) {
                float invMax = 1.0f / trcMax;
                for (auto& v : trc) v = std::clamp(v * invMax, 0.0f, 1.0f);
            }
        };
        normalizeTRC(outData.trcR);
        normalizeTRC(outData.trcG);
        normalizeTRC(outData.trcB);
    }

    // Read description tag
    if (auto* tag = findTag(MakeSig("desc"))) {
        if (tag->offset + 12 <= fileSize) {
            const uint8_t* p = d + tag->offset;
            uint32_t typeSig = ReadBE32(p);
            if (typeSig == MakeSig("mluc") && tag->offset + 28 <= fileSize) {
                uint32_t strLen = ReadBE32(p + 20);
                uint32_t strOff = ReadBE32(p + 24);
                if ((size_t)tag->offset + strOff + strLen <= fileSize) {
                    for (uint32_t i = 0; i < strLen / 2; i++) {
                        outData.description += (wchar_t)ReadBE16(p + strOff + i * 2);
                    }
                }
            }
        }
    }

    // Read lumi tag (peak luminance in cd/m²)
    if (auto* tag = findTag(MakeSig("lumi"))) {
        if (tag->offset + 20 <= fileSize) {
            const uint8_t* p = d + tag->offset;
            if (ReadBE32(p) == MakeSig("XYZ ")) {
                // Y component = luminance in cd/m²
                float Y = ReadS15Fixed16(p + 12);
                if (Y > 0.0f) {
                    outData.luminance = Y;
                    outData.hasLuminance = true;
                }
            }
        }
    }

    std::cout << "ICC: Read profile, primaries=" << outData.hasPrimaries
              << " trc=" << outData.hasTRC << std::endl;
    return outData.hasPrimaries || outData.hasTRC;
}

// ============================================================================
// SECTION: Grayscale Extraction
// ============================================================================

bool ExtractGrayscaleFromICC(const ICCProfileData& icc, GrayscaleSettings& outGrayscale, bool isHDR) {
    if (!icc.hasTRC) return false;

    // Average R/G/B curves
    size_t curveLen = (std::min)({icc.trcR.size(), icc.trcG.size(), icc.trcB.size()});
    if (curveLen < 2) return false;

    std::vector<float> avgCurve(curveLen);
    for (size_t i = 0; i < curveLen; i++) {
        avgCurve[i] = (icc.trcR[i] + icc.trcG[i] + icc.trcB[i]) / 3.0f;
    }

    int N = outGrayscale.pointCount;
    outGrayscale.points.resize(N);

    if (isHDR) {
        // HDR: SDR ICC TRC cannot provide meaningful HDR grayscale correction.
        // The TRC describes the display's SDR-mode response (which includes the calibration
        // target — gamma 2.2, BT.1886, S-curve, etc.), not the display's HDR PQ tracking.
        // HDR uses a completely different signal domain (PQ) with different panel behavior
        // (tone mapping, local dimming, ABL). Primaries transfer (same physical panel) but
        // grayscale does not. For HDR grayscale correction, use measurements taken in HDR mode.
        // For full volumetric correction from SDR ICC, use DisplayCal's 3DLUT maker to generate
        // a PQ BT.2020 .cube file and load it in the 3D LUT tab.
        return false;  // No grayscale extracted for HDR
    } else {
        // SDR: sqrt-distribution points
        // ICC TRC: input signal -> linear light output
        // GrayscaleSettings: points[i] = output linear value for input (i/(N-1))^2
        // TRC index is in signal domain, so convert linear to signal using gamma
        float gamma = (icc.gamma > 0.1f) ? icc.gamma : 2.2f;
        for (int i = 0; i < N; i++) {
            float t = (float)i / (float)(N - 1);
            float inputLinear = t * t;  // Input level (sqrt distribution)

            // Convert linear to signal domain for TRC indexing
            float signal = powf(inputLinear, 1.0f / gamma);
            float idx = signal * (float)(curveLen - 1);
            int i0 = (int)idx;
            int i1 = (std::min)(i0 + 1, (int)curveLen - 1);
            float frac = idx - floorf(idx);
            float iccVal = avgCurve[i0] + (avgCurve[i1] - avgCurve[i0]) * frac;
            outGrayscale.points[i] = iccVal;
        }
        outGrayscale.enabled = true;
    }

    return true;
}

bool ExtractGrayscaleFromCube(const std::wstring& path, GrayscaleSettings& outGrayscale) {
    // Load cube file
    std::vector<float> lutData;
    int lutSize = 0;

    // Forward declare - defined in lut.cpp
    extern bool LoadLUT(const std::wstring& path, std::vector<float>& data, int& lutSize);

    if (!LoadLUT(path, lutData, lutSize) || lutSize < 2) return false;

    int N = outGrayscale.pointCount;
    outGrayscale.points.resize(N);

    // Sample neutral axis (R=G=B diagonal) at sqrt-spaced input levels.
    // The shader's ApplyGrayscaleCorrection uses sqrt indexing: idx = sqrt(Y) * (N-1),
    // so point i corresponds to input Y = (i/(N-1))^2.
    for (int i = 0; i < N; i++) {
        float sqrtT = (float)i / (float)(N - 1);  // sqrt-space index 0-1
        float t = sqrtT * sqrtT;  // Input level in gamma space

        // Trilinear interpolation at (t, t, t) in the LUT
        float pos = t * (float)(lutSize - 1);
        int i0 = (int)floorf(pos);
        int i1 = (std::min)(i0 + 1, lutSize - 1);
        float frac = pos - floorf(pos);

        // 8 corners of the cube cell (all on diagonal, so r=g=b indices are same)
        auto sampleLUT = [&](int r, int g, int b) -> float {
            int idx = (r + g * lutSize + b * lutSize * lutSize) * 4;  // RGBA
            if (idx + 2 >= (int)lutData.size()) return t;
            return (lutData[idx] + lutData[idx + 1] + lutData[idx + 2]) / 3.0f;
        };

        // Trilinear interpolate along diagonal
        float v000 = sampleLUT(i0, i0, i0);
        float v100 = sampleLUT(i1, i0, i0);
        float v010 = sampleLUT(i0, i1, i0);
        float v001 = sampleLUT(i0, i0, i1);
        float v110 = sampleLUT(i1, i1, i0);
        float v101 = sampleLUT(i1, i0, i1);
        float v011 = sampleLUT(i0, i1, i1);
        float v111 = sampleLUT(i1, i1, i1);

        float c00 = v000 + (v100 - v000) * frac;
        float c01 = v001 + (v101 - v001) * frac;
        float c10 = v010 + (v110 - v010) * frac;
        float c11 = v011 + (v111 - v011) * frac;
        float c0 = c00 + (c10 - c00) * frac;
        float c1 = c01 + (c11 - c01) * frac;
        float output = c0 + (c1 - c0) * frac;

        // For SDR sqrt-distribution: points[i] should be the output value
        // The expected identity output = t, so this captures any LUT deviation
        outGrayscale.points[i] = std::clamp(output, 0.0f, 1.0f);
    }

    outGrayscale.enabled = true;
    return true;
}

// ============================================================================
// SECTION: 1D Cube Loading
// ============================================================================

bool Load1DCubeLUT(const std::wstring& path, std::vector<float>& outR, std::vector<float>& outG, std::vector<float>& outB) {
    std::ifstream file(path);
    if (!file.is_open()) {
        std::wcerr << L"Failed to open 1D LUT file: " << path << std::endl;
        return false;
    }

    int lutSize = 0;
    std::vector<float> tempR, tempG, tempB;
    std::string line;

    while (std::getline(file, line)) {
        if (line.empty() || line[0] == '#') continue;
        if (line.find("TITLE") == 0) continue;
        if (line.find("DOMAIN_MIN") == 0) continue;
        if (line.find("DOMAIN_MAX") == 0) continue;

        // Must be a 1D LUT
        if (line.find("LUT_3D_SIZE") == 0) {
            std::cerr << "Error: File contains a 3D LUT, expected 1D" << std::endl;
            return false;
        }

        if (line.find("LUT_1D_SIZE") == 0) {
            std::istringstream iss(line.substr(11));
            iss.imbue(std::locale::classic());
            iss >> lutSize;
            if (lutSize < 2 || lutSize > 65536) {
                std::cerr << "Invalid 1D LUT size: " << lutSize << std::endl;
                return false;
            }
            tempR.reserve(lutSize);
            tempG.reserve(lutSize);
            tempB.reserve(lutSize);
            continue;
        }

        if (line.find("LUT_1D_INPUT_RANGE") == 0) continue;

        // Parse R G B triplets
        std::istringstream iss(line);
        iss.imbue(std::locale::classic());
        float r, g, b;
        if (iss >> r >> g >> b) {
            tempR.push_back(r);
            tempG.push_back(g);
            tempB.push_back(b);
        }
    }

    if (tempR.empty()) {
        std::cerr << "No 1D LUT data found in file" << std::endl;
        return false;
    }

    // If no LUT_1D_SIZE header, infer from data count
    if (lutSize == 0) lutSize = (int)tempR.size();

    if ((int)tempR.size() != lutSize) {
        std::cerr << "1D LUT: expected " << lutSize << " entries, got " << tempR.size() << std::endl;
        return false;
    }

    outR = std::move(tempR);
    outG = std::move(tempG);
    outB = std::move(tempB);
    std::cout << "Loaded 1D cube LUT: " << lutSize << " entries per channel" << std::endl;
    return true;
}
