// DesktopLUT - tests/fald_panel_legacy_loader.h
// FROZEN COPY of the panel-file loader as it was before the live-delivery refactor (main e30d567,
// shared/fald_panel.cpp LoadFaldPanelParams and its helpers), renamed Legacy*. Do not edit: it is the reference
// tests/test_fald_live.cpp holds ParseFaldPanelParams to, bit for bit, so the refactor (and any later change of the
// parser) cannot silently change what a staged or delivered file gives the layer. Generated from `git show
// e30d567:shared/fald_panel.cpp`; only the names were changed.
#pragma once
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>
#include "../shared/fald_panel.h"

namespace fald_legacy {

static const uint32_t Legacy_FALD_MAGIC = 0x464C4431u;   // 'FLD1' (32-word header)
static const uint32_t Legacy_FALD_MAGIC2 = 0x464C4432u;  // 'FLD2' (40-word header: + pedestal colour, DLC export.py)
static const uint32_t Legacy_FALD_MAGIC3 = 0x464C4433u;  // 'FLD3' (48-word header: + signal transfer words 40/41; SDR/ACM fits)
static const uint32_t Legacy_FALD_MAGIC4 = 0x464C4434u;  // 'FLD4' (104-word header: + black-frame LED boost block, words 48-103)
static const size_t Legacy_FALD_BOOST_WORD_COUNT = 48;   // FLD4 word 48: step count; 49-52: activation rule; 53: zone rule kind
static const size_t Legacy_FALD_BOOST_WORD_RULE = 53;
static const size_t Legacy_FALD_BOOST_WORD_LUT = 56;     // FLD4 words 56..103: 24 x (zone fraction lo, boost)

static size_t Legacy_FaldHeaderBytes(uint32_t magic) {
    if (magic == Legacy_FALD_MAGIC4) return 104 * 4;
    if (magic == Legacy_FALD_MAGIC3) return 48 * 4;
    if (magic == Legacy_FALD_MAGIC2) return 40 * 4;
    return 32 * 4;
}
static bool Legacy_FaldMagicKnown(uint32_t magic) {
    return magic == Legacy_FALD_MAGIC || magic == Legacy_FALD_MAGIC2 || magic == Legacy_FALD_MAGIC3 || magic == Legacy_FALD_MAGIC4;
}
static bool Legacy_FaldMagicLong(uint32_t magic) { return magic == Legacy_FALD_MAGIC3 || magic == Legacy_FALD_MAGIC4; }

inline bool LegacyLoadFaldPanelParams(const std::wstring& path, FaldPanelParams& out, std::string& err) {
    out = FaldPanelParams{};   // Build loads into long-lived resources: an earlier FLD3 must not leave transfer=gamma behind
    std::ifstream f(path, std::ios::binary);
    if (!f) { err = "cannot open params file"; return false; }
    std::vector<char> buf((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (buf.size() < 128) { err = "params file too short"; return false; }
    const uint32_t* u = reinterpret_cast<const uint32_t*>(buf.data());
    const float* fl = reinterpret_cast<const float*>(buf.data());
    if (!Legacy_FaldMagicKnown(u[0])) { err = "bad magic (expected FLD1, FLD2, FLD3 or FLD4)"; return false; }
    const size_t headerBytes = Legacy_FaldHeaderBytes(u[0]);
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
    const bool pedBlock = (u[0] == Legacy_FALD_MAGIC2) ||
                          (Legacy_FaldMagicLong(u[0]) && (u[32] != 0 || u[33] != 0 || u[34] != 0 || u[35] != 0));
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
    if (Legacy_FaldMagicLong(u[0])) {                                                              // words 40/41: signal transfer + SDR gamma
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
    if (u[0] == Legacy_FALD_MAGIC4) {                                                              // words 48-103: black-frame LED boost
        const uint32_t n = u[Legacy_FALD_BOOST_WORD_COUNT];
        if (n > FALD_BOOST_MAX_STEPS) { err = "implausible boost step count"; return false; }
        if (n > 0) {                                                                        // 0 = no boost (the block is ignored)
            const float litNits = fl[49], litFrac = fl[50], dimNits = fl[51], dimFrac = fl[52];
            if (!(litNits >= 0.0f && litNits <= 10000.0f) || !(dimNits >= 0.0f && dimNits <= 10000.0f) ||
                !(litFrac >= 0.0f && litFrac < 1.0f) || !(dimFrac >= 0.0f && dimFrac < 1.0f)) {
                err = "implausible boost activation words"; return false;
            }
            // word 53: the zone rule (C12b). 0 = LIT-or-DIM — what every earlier FLD4 file says (the word was reserved,
            // written zero): words 54 / 55 are then not read. 1 = LIT-or-MEAN with words 54 (gamma) / 55 (threshold).
            const uint32_t rule = u[Legacy_FALD_BOOST_WORD_RULE];
            float meanGamma = out.boostMeanGamma, meanThresh = out.boostMeanThresh;        // the defaults (unused for rule 0)
            if (rule > FALD_BOOST_RULE_MEAN) { err = "unknown boost zone rule word (expected 0 = LIT-or-DIM or 1 = LIT-or-MEAN)"; return false; }
            if (rule == FALD_BOOST_RULE_MEAN) {
                meanGamma = fl[Legacy_FALD_BOOST_WORD_RULE + 1]; meanThresh = fl[Legacy_FALD_BOOST_WORD_RULE + 2];
                if (!(meanGamma > 0.0f && meanGamma <= 4.0f) || !(meanThresh > 0.0f && std::isfinite(meanThresh))) {
                    err = "implausible boost mean-rule words (gamma in (0, 4], threshold a finite number > 0)"; return false;
                }
            }
            for (uint32_t i = 0; i < n; i++) {
                const float lo = fl[Legacy_FALD_BOOST_WORD_LUT + 2 * i], val = fl[Legacy_FALD_BOOST_WORD_LUT + 2 * i + 1];
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

}  // namespace fald_legacy
