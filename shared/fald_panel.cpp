// DesktopLUT - fald_panel.cpp
// The FALD panel parameter file reader. See fald_panel.h. Moved here out of src/fald.cpp so the
// overlay path and the DWM hook parse the file with the same code.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include "fald_panel.h"

#include <cmath>
#include <fstream>
#include <iterator>

static const uint32_t FALD_MAGIC = 0x464C4431u;   // 'FLD1' (32-word header)
static const uint32_t FALD_MAGIC2 = 0x464C4432u;  // 'FLD2' (40-word header: + pedestal colour, DLC export.py)
static const uint32_t FALD_MAGIC3 = 0x464C4433u;  // 'FLD3' (48-word header: + signal transfer words 40/41; SDR/ACM fits)
static const uint32_t FALD_MAGIC4 = 0x464C4434u;  // 'FLD4' (104-word header: + black-frame LED boost block, words 48-103)
static const size_t FALD_BOOST_WORD_COUNT = 48;   // FLD4 word 48: step count; 49-52: activation rule; 53: zone rule kind
static const size_t FALD_BOOST_WORD_RULE = 53;
static const size_t FALD_BOOST_WORD_LUT = 56;     // FLD4 words 56..103: 24 x (zone fraction lo, boost)

static size_t FaldHeaderBytes(uint32_t magic) {
    if (magic == FALD_MAGIC4) return 104 * 4;
    if (magic == FALD_MAGIC3) return 48 * 4;
    if (magic == FALD_MAGIC2) return 40 * 4;
    return 32 * 4;
}
static bool FaldMagicKnown(uint32_t magic) {
    return magic == FALD_MAGIC || magic == FALD_MAGIC2 || magic == FALD_MAGIC3 || magic == FALD_MAGIC4;
}
static bool FaldMagicLong(uint32_t magic) { return magic == FALD_MAGIC3 || magic == FALD_MAGIC4; }

bool LoadFaldPanelParams(const std::wstring& path, FaldPanelParams& out, std::string& err) {
    out = FaldPanelParams{};   // Build loads into long-lived resources: an earlier FLD3 must not leave transfer=gamma behind
    std::ifstream f(path, std::ios::binary);
    if (!f) { err = "cannot open params file"; return false; }
    std::vector<char> buf((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (buf.size() < 128) { err = "params file too short"; return false; }
    const uint32_t* u = reinterpret_cast<const uint32_t*>(buf.data());
    const float* fl = reinterpret_cast<const float*>(buf.data());
    if (!FaldMagicKnown(u[0])) { err = "bad magic (expected FLD1, FLD2, FLD3 or FLD4)"; return false; }
    const size_t headerBytes = FaldHeaderBytes(u[0]);
    if (buf.size() < headerBytes) { err = "params file too short"; return false; }
    out.cols = u[1]; out.rows = u[2]; out.sub = u[3]; out.cellW = u[4]; out.cellH = u[5];
    out.originX = u[6]; out.originY = u[7];
    out.reachTrueC = u[8]; out.reachTrueR = u[9]; out.reachEstC = u[10]; out.reachEstR = u[11];
    out.curveN = u[12];
    out.white = fl[13]; out.tmin = fl[14]; out.area0 = fl[15];
    out.w[0] = fl[16]; out.w[1] = fl[17]; out.w[2] = fl[18];
    out.gainMin = fl[19]; out.gainMax = fl[20]; out.driveFloor = fl[21];
    out.curveLogMin = fl[22]; out.curveLogMax = fl[23]; out.estPhasePx = fl[24]; out.estPhasePy = fl[25];
    if (fl[27] > 0.0f && fl[27] > fl[26]) { out.fadeLo = fl[26]; out.fadeHi = fl[27]; }   // reserved words in older files = defaults
    if (u[28] != 0) out.gainSmoothCells = fl[28];                                          // word 28: gain low-pass sigma (cells); absent = default
    if (u[29] != 0 || u[30] != 0) {                                                        // words 29/30: pixel-luminance fade (nits); absent = defaults
        if (fl[30] > fl[29] && fl[29] >= 0.0f) { out.lumFadeLo = fl[29]; out.lumFadeHi = fl[30]; }
        else { err = "implausible lum_fade words"; return false; }
    }
    // words 32-34: pedestal colour, 35: validated mode. Always present in FLD2; in FLD3/FLD4 all-zero words 32-35 mean
    // "no pedestal colour" (an SDR fit without one still needs the transfer words, so the block is optional there).
    const bool pedBlock = (u[0] == FALD_MAGIC2) ||
                          (FaldMagicLong(u[0]) && (u[32] != 0 || u[33] != 0 || u[34] != 0 || u[35] != 0));
    if (pedBlock) {
        out.pedRGB[0] = fl[32]; out.pedRGB[1] = fl[33]; out.pedRGB[2] = fl[34];
        out.pedModeFile = u[35]; out.hasPedColour = true;
        if (u[36] != 0) {                                                                   // word 36: colour-part gain (0 = default)
            out.chromaGain = fl[36];
            if (!(out.chromaGain > 0.0f && out.chromaGain <= 100.0f)) { err = "implausible pedestal chroma gain"; return false; }
            if (u[37] == 0 && u[38] == 0) { out.chromaLo = 0.0f; out.chromaHi = 0.0f; }   // no fade on the colour part
            else if (fl[38] > fl[37] && fl[37] >= 0.0f) { out.chromaLo = fl[37]; out.chromaHi = fl[38]; }
            else { err = "implausible pedestal chroma fade words"; return false; }
        }
        float lum = out.w[0] * out.pedRGB[0] + out.w[1] * out.pedRGB[1] + out.w[2] * out.pedRGB[2];
        if (!(out.pedRGB[0] >= 0.0f && out.pedRGB[1] >= 0.0f && out.pedRGB[2] >= 0.0f) ||
            !(out.pedRGB[0] <= 8.0f && out.pedRGB[1] <= 8.0f && out.pedRGB[2] <= 8.0f) ||
            !(lum > 0.9f && lum < 1.1f) || out.pedModeFile > 1) {
            err = "implausible pedestal colour words"; return false;
        }
    }
    if (FaldMagicLong(u[0])) {                                                              // words 40/41: signal transfer + SDR gamma
        out.hasTransfer = true;
        out.transfer = u[40];
        if (out.transfer == FALD_TRANSFER_GAMMA) {
            out.sdrGamma = fl[41];
            if (!(out.sdrGamma >= 1.0f && out.sdrGamma <= 4.0f)) { err = "implausible sdr_gamma word"; return false; }
        } else if (out.transfer == FALD_TRANSFER_PQ) {
            out.sdrGamma = 0.0f;                                                            // word 41 unused for PQ
        } else {
            err = "unknown transfer word (expected 0 = PQ or 1 = gamma)"; return false;
        }
    }
    if (u[0] == FALD_MAGIC4) {                                                              // words 48-103: black-frame LED boost
        const uint32_t n = u[FALD_BOOST_WORD_COUNT];
        if (n > FALD_BOOST_MAX_STEPS) { err = "implausible boost step count"; return false; }
        if (n > 0) {                                                                        // 0 = no boost (the block is ignored)
            const float litNits = fl[49], litFrac = fl[50], dimNits = fl[51], dimFrac = fl[52];
            if (!(litNits >= 0.0f && litNits <= 10000.0f) || !(dimNits >= 0.0f && dimNits <= 10000.0f) ||
                !(litFrac >= 0.0f && litFrac < 1.0f) || !(dimFrac >= 0.0f && dimFrac < 1.0f)) {
                err = "implausible boost activation words"; return false;
            }
            // word 53: the zone rule (C12b). 0 = LIT-or-DIM — what every earlier FLD4 file says (the word was reserved,
            // written zero): words 54 / 55 are then not read. 1 = LIT-or-MEAN with words 54 (gamma) / 55 (threshold).
            const uint32_t rule = u[FALD_BOOST_WORD_RULE];
            float meanGamma = out.boostMeanGamma, meanThresh = out.boostMeanThresh;        // the defaults (unused for rule 0)
            if (rule > FALD_BOOST_RULE_MEAN) { err = "unknown boost zone rule word (expected 0 = LIT-or-DIM or 1 = LIT-or-MEAN)"; return false; }
            if (rule == FALD_BOOST_RULE_MEAN) {
                meanGamma = fl[FALD_BOOST_WORD_RULE + 1]; meanThresh = fl[FALD_BOOST_WORD_RULE + 2];
                if (!(meanGamma > 0.0f && meanGamma <= 4.0f) || !(meanThresh > 0.0f && std::isfinite(meanThresh))) {
                    err = "implausible boost mean-rule words (gamma in (0, 4], threshold a finite number > 0)"; return false;
                }
            }
            for (uint32_t i = 0; i < n; i++) {
                const float lo = fl[FALD_BOOST_WORD_LUT + 2 * i], val = fl[FALD_BOOST_WORD_LUT + 2 * i + 1];
                if (!(lo >= 0.0f && lo <= 1.0f) || (i > 0 && !(lo > out.boostLo[i - 1])) || !(val >= 0.5f && val <= 2.0f)) {
                    err = "implausible boost LUT words (steps must ascend, fractions 0..1, boosts 0.5..2)"; return false;
                }
                out.boostLo[i] = lo; out.boostVal[i] = val;
            }
            out.boostN = n; out.hasBoost = true;
            out.boostLitNits = litNits; out.boostLitFrac = litFrac; out.boostDimNits = dimNits; out.boostDimFrac = dimFrac;
            out.boostRule = rule; out.boostMeanGamma = meanGamma; out.boostMeanThresh = meanThresh;
        }
    }
    if (out.cols == 0 || out.rows == 0 || out.sub == 0 || out.sub > 16 || out.cellW == 0 || out.cellH == 0 ||
        out.curveN < 16 || out.curveN > 16384 || out.white <= 0 || out.cols > 512 || out.rows > 512 ||
        out.reachTrueC > 64 || out.reachTrueR > 64 || out.reachEstC > 64 || out.reachEstR > 64 ||
        !(out.curveLogMax > out.curveLogMin) || !(out.gainMin > 0 && out.gainMin <= out.gainMax) ||
        !(out.tmin >= 0) || !(out.area0 > 0) || !(out.driveFloor >= 0)) {
        err = "implausible header"; return false;
    }
    size_t nTrue = (size_t)out.sub * out.sub * (2 * out.reachTrueR + 1) * (2 * out.reachTrueC + 1);
    size_t nEst = (size_t)out.sub * out.sub * (2 * out.reachEstR + 1) * (2 * out.reachEstC + 1);
    size_t need = headerBytes + 4 * ((size_t)out.curveN + nTrue + nEst);
    if (buf.size() != need) { err = "size mismatch (" + std::to_string(buf.size()) + " vs " + std::to_string(need) + ")"; return false; }
    const float* p = reinterpret_cast<const float*>(buf.data() + headerBytes);
    out.curve.assign(p, p + out.curveN); p += out.curveN;
    out.kTrue.assign(p, p + nTrue); p += nTrue;
    out.kEst.assign(p, p + nEst);
    return true;
}

bool FaldPanelFileHasPedColour(const std::wstring& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    uint32_t head[36] = {};
    f.read(reinterpret_cast<char*>(head), sizeof(head));
    if (f.gcount() < 4) return false;
    if (head[0] == FALD_MAGIC2) return true;
    if (FaldMagicLong(head[0]) && f.gcount() == (std::streamsize)sizeof(head))
        return head[32] != 0 || head[33] != 0 || head[34] != 0 || head[35] != 0;
    return false;
}

bool FaldPanelFileHasBoost(const std::wstring& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    uint32_t head[FALD_BOOST_WORD_COUNT + 1] = {};
    f.read(reinterpret_cast<char*>(head), sizeof(head));
    if (f.gcount() != (std::streamsize)sizeof(head) || head[0] != FALD_MAGIC4) return false;
    return head[FALD_BOOST_WORD_COUNT] >= 1 && head[FALD_BOOST_WORD_COUNT] <= FALD_BOOST_MAX_STEPS;
}

unsigned int FaldBoostZoneThreshold(float lo, unsigned int zonesTotal) {
    // step applies when N / Z >= lo  <=>  N >= lo * Z. lo went through float32 (relative error 6e-8): the tolerance
    // keeps an edge that IS a zone count (e.g. 145 / 2304) at that count instead of one above. < 0.5 zone up to the
    // loader's 512 x 512 lattice limit.
    const double z = (double)zonesTotal;
    const double t = std::ceil((double)lo * z - (1e-3 + 1e-6 * z));
    return t <= 0.0 ? 0u : (t >= 4294967295.0 ? 4294967295u : (unsigned int)t);
}

float FaldBoostOfCount(const FaldPanelParams& p, unsigned int activeZones) {
    float b = 1.0f;
    if (!p.hasBoost) return b;
    const unsigned int zones = p.cols * p.rows;
    for (uint32_t i = 0; i < p.boostN && i < FALD_BOOST_MAX_STEPS; i++) {
        if (activeZones < FaldBoostZoneThreshold(p.boostLo[i], zones)) break;
        b = p.boostVal[i];
    }
    return b;
}

bool FaldBoostZoneActive(const FaldPanelParams& p, const float* maxChannelNits, size_t n) {
    if (!maxChannelNits || n == 0) return false;
    size_t lit = 0, dim = 0;
    float powSum = 0.0f;
    for (size_t i = 0; i < n; i++) {
        const float mc = maxChannelNits[i];
        if (mc > p.boostLitNits) lit++;
        if (mc > p.boostDimNits) dim++;
        if (p.boostRule == FALD_BOOST_RULE_MEAN && mc > 0.0f) powSum += std::exp(p.boostMeanGamma * std::log(mc));   // as the HLSL
    }
    const float litF = (float)lit / (float)n, dimF = (float)dim / (float)n;
    const bool second = (p.boostRule == FALD_BOOST_RULE_MEAN) ? (powSum / (float)n >= p.boostMeanThresh) : (dimF > p.boostDimFrac);
    return litF > p.boostLitFrac || second;
}

bool FaldPanelFileTransfer(const std::wstring& path, uint32_t& transfer) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    uint32_t head[41] = {};
    f.read(reinterpret_cast<char*>(head), sizeof(head));
    if (f.gcount() < 4) return false;
    if (head[0] == FALD_MAGIC || head[0] == FALD_MAGIC2) { transfer = FALD_TRANSFER_PQ; return true; }
    if (FaldMagicLong(head[0]) && f.gcount() == (std::streamsize)sizeof(head) &&
        (head[40] == FALD_TRANSFER_PQ || head[40] == FALD_TRANSFER_GAMMA)) { transfer = head[40]; return true; }
    return false;
}

bool FaldTransferMatchesMode(uint32_t transfer, bool monitorHdr) {
    return monitorHdr ? (transfer == FALD_TRANSFER_PQ) : (transfer == FALD_TRANSFER_GAMMA);
}

bool FaldLatticeFits(const FaldPanelParams& p, int width, int height) {
    if (width <= 0 || height <= 0) return false;
    unsigned long long right = (unsigned long long)p.originX + (unsigned long long)p.cols * p.cellW;
    unsigned long long bottom = (unsigned long long)p.originY + (unsigned long long)p.rows * p.cellH;
    return right <= (unsigned long long)width && bottom <= (unsigned long long)height;
}

float FaldGlowReqCeil(const FaldPanelParams& p) {
    float c = FALD_GLOW_REQ_FLOOR_FRAC * p.driveFloor;
    if (p.hasBoost) { const float lit = FALD_GLOW_REQ_LIT_FRAC * p.boostLitNits; if (lit < c) c = lit; }
    return c;
}

bool FaldPanelFileStamp(const std::wstring& path, unsigned long long& size, unsigned long long& mtime) {
    WIN32_FILE_ATTRIBUTE_DATA fad;
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &fad)) return false;
    size = ((unsigned long long)fad.nFileSizeHigh << 32) | fad.nFileSizeLow;
    mtime = ((unsigned long long)fad.ftLastWriteTime.dwHighDateTime << 32) | fad.ftLastWriteTime.dwLowDateTime;
    return true;
}
