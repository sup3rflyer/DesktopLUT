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
#include <cstring>
#include <fstream>
#include <iterator>
#include <utility>

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
    return ParseFaldPanelParams(buf.data(), buf.size(), out, err);
}

// out = FaldPanelParams{}, except that the three tables keep their allocations (emptied): a caller that parses into the
// same object again (the DWM hook's spare in shared/fald_panel_live.cpp) reuses memory that is already faulted in.
// Nothing of the previous file survives — the tables are empty, every other field is its default.
static void ResetFaldPanelParams(FaldPanelParams& out) {
    std::vector<float> curve = std::move(out.curve), kTrue = std::move(out.kTrue), kEst = std::move(out.kEst);
    out = FaldPanelParams{};
    curve.clear(); kTrue.clear(); kEst.clear();
    out.curve = std::move(curve); out.kTrue = std::move(kTrue); out.kEst = std::move(kEst);
}

bool ParseFaldPanelParams(const void* data, size_t bytes, FaldPanelParams& out, std::string& err) {
    ResetFaldPanelParams(out);
    if (!data || bytes < 128) { err = "params file too short"; return false; }
    // The image may sit at any alignment (a delivery block's payload, a file buffer): the header words are copied out
    // once into an aligned array, the tables with memcpy below. Bit for bit what reading the words in place gives.
    const size_t headerWordsMax = 104;   // FLD4, the longest header
    uint32_t hw[headerWordsMax] = {};
    std::memcpy(hw, data, (bytes < sizeof(hw) ? bytes : sizeof(hw)));
    const uint32_t* u = hw;
    const float* fl = reinterpret_cast<const float*>(hw);
    if (!FaldMagicKnown(u[0])) { err = "bad magic (expected FLD1, FLD2, FLD3 or FLD4)"; return false; }
    const size_t headerBytes = FaldHeaderBytes(u[0]);
    if (bytes < headerBytes) { err = "params file too short"; return false; }
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
                if (!(lo >= 0.0f && lo <= 1.0f) || (i > 0 && !(lo > out.boostLo[i - 1])) ||
                    !(val >= FALD_BOOST_STEP_MIN && val <= FALD_BOOST_STEP_MAX)) {
                    err = "implausible boost LUT words (steps must ascend, fractions 0..1, boosts 0.25..4)"; return false;
                }
                out.boostLo[i] = lo; out.boostVal[i] = val;
            }
            out.boostN = n; out.hasBoost = true;
            out.boostLitNits = litNits; out.boostLitFrac = litFrac; out.boostDimNits = dimNits; out.boostDimFrac = dimFrac;
            out.boostRule = rule; out.boostMeanGamma = meanGamma; out.boostMeanThresh = meanThresh;
        }
    }
    // The lattice (docs/fald-fractional-pitch.md): words 4/5 = whole-pixel cells with word 31 = 0 (every file before
    // 2026-10-19 — word 31 was reserved, written 0), OR words 4/5 = 0 with word 31 = the lattice EXTENT latW | latH << 16
    // (a fractional pitch). An exe from before 2026-10-19 refuses the second form as an implausible header (cellW == 0) —
    // deliberately: it would run the zones on a drifting whole-pixel lattice. A zero cell word WITHOUT an extent is the
    // implausible header it always was.
    const uint32_t extentWord = u[31];
    const bool noLattice = (out.cellW == 0 || out.cellH == 0) && extentWord == 0;
    if (out.cols == 0 || out.rows == 0 || out.sub == 0 || out.sub > 16 || noLattice ||
        out.curveN < 16 || out.curveN > 16384 || out.white <= 0 || out.cols > 512 || out.rows > 512 ||
        out.reachTrueC > 64 || out.reachTrueR > 64 || out.reachEstC > 64 || out.reachEstR > 64 ||
        !(out.curveLogMax > out.curveLogMin) || !(out.gainMin > 0 && out.gainMin <= out.gainMax) ||
        !(out.tmin >= 0) || !(out.area0 > 0) || !(out.driveFloor >= 0)) {
        err = "implausible header"; return false;
    }
    if (extentWord == 0) {                                                                  // whole-pixel cells
        const unsigned long long lw = (unsigned long long)out.cols * out.cellW, lh = (unsigned long long)out.rows * out.cellH;
        if (lw > FALD_LATTICE_MAX_PX || lh > FALD_LATTICE_MAX_PX) {
            err = "implausible lattice words (a lattice of more than 65535 px)"; return false;
        }
        out.latW = (uint32_t)lw; out.latH = (uint32_t)lh;
    } else {                                                                                // the extent: a rational pitch
        if (out.cellW != 0 || out.cellH != 0) {
            err = "implausible lattice words (cell size words 4/5 and an extent word 31 together)"; return false;
        }
        const uint32_t lw = extentWord & 0xFFFFu, lh = extentWord >> 16;
        if (lw < out.cols || lh < out.rows) {
            err = "implausible lattice words (an extent of fewer pixels than zones)"; return false;
        }
        out.latW = lw; out.latH = lh;
        out.cellW = (lw + out.cols - 1) / out.cols;                                         // the widest zone (sweep slices)
        out.cellH = (lh + out.rows - 1) / out.rows;
    }
    size_t nTrue = (size_t)out.sub * out.sub * (2 * out.reachTrueR + 1) * (2 * out.reachTrueC + 1);
    size_t nEst = (size_t)out.sub * out.sub * (2 * out.reachEstR + 1) * (2 * out.reachEstC + 1);
    size_t need = headerBytes + 4 * ((size_t)out.curveN + nTrue + nEst);
    if (bytes != need) { err = "size mismatch (" + std::to_string(bytes) + " vs " + std::to_string(need) + ")"; return false; }
    const unsigned char* p = static_cast<const unsigned char*>(data) + headerBytes;
    out.curve.resize(out.curveN); std::memcpy(out.curve.data(), p, 4 * (size_t)out.curveN); p += 4 * (size_t)out.curveN;
    out.kTrue.resize(nTrue);      std::memcpy(out.kTrue.data(), p, 4 * nTrue);              p += 4 * nTrue;
    out.kEst.resize(nEst);        std::memcpy(out.kEst.data(), p, 4 * nEst);
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

unsigned int FaldConvRowStride(unsigned int Wt, unsigned int S) { return 4u + ((Wt * S + 3u) & ~3u); }
unsigned int FaldConvGroups(unsigned int sub, unsigned int S) { return (sub * sub + S - 1u) / S; }

std::vector<float> FaldConvTableLayout(const std::vector<float>& table, unsigned int sub, unsigned int Wt, unsigned int Ht, unsigned int S) {
    const unsigned int groups = FaldConvGroups(sub, S), stride = FaldConvRowStride(Wt, S);
    std::vector<float> out((size_t)groups * Ht * stride, 0.0f);
    bool nonNegative = true;   // a negative (or -0) tap: a skipped +0 term could change the sign of a zero sum -> full rows
    for (float v : table) if (std::signbit(v)) { nonNegative = false; break; }
    for (unsigned int g = 0; g < groups; g++)
        for (unsigned int jj = 0; jj < Ht; jj++) {
            float* row = &out[((size_t)g * Ht + jj) * stride];
            int lo = (int)Wt, hi = -1;
            for (unsigned int s = 0; s < S; s++) {
                const unsigned int so = g * S + s;
                if (so >= sub * sub) continue;
                for (unsigned int ii = 0; ii < Wt; ii++) {
                    const float v = table[((size_t)so * Ht + jj) * Wt + ii];
                    row[4u + ii * S + s] = v;
                    if (v != 0.0f) { if ((int)ii < lo) lo = (int)ii; if ((int)ii > hi) hi = (int)ii; }
                }
            }
            if (!nonNegative) { lo = 0; hi = (int)Wt - 1; }
            if (hi < lo) { lo = 1; hi = 0; }   // an all-zero row: an empty range
            std::memcpy(&row[0], &lo, sizeof(int));
            std::memcpy(&row[1], &hi, sizeof(int));
        }
    return out;
}

int g_faldConvVariantOverride = -1;
int g_faldGainFusedOverride = -1;
int g_faldPartialOverride = -1;

unsigned int FaldConvVariant(unsigned int cols, unsigned int rows, unsigned int sub) {
    (void)sub;
    if (g_faldConvVariantOverride >= 0 && g_faldConvVariantOverride < 4) return (unsigned int)g_faldConvVariantOverride;
    // The policy (RTX 5090, 2026-10-22, DLC/results/fald_core_speed_2026-10-22): a thread per sub-offset (S = 1) keeps
    // the most threads in flight, which a large GPU needs on a small lattice (S = 2 lost 50 % at 1080p / 1440p); on a
    // 48 x 48 lattice S = 2 won 14 % of the pass (73k threads still fill the 5090). S = 4 / 8 lost everywhere on it — they
    // are for devices with far fewer thread slots, where the loads saved matter; a measured auto-tune is the long-term
    // rule, this is the measured line on one device.
    return (cols * rows >= 2048u) ? 1u : 0u;
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
    if (p.cols == 0 || p.rows == 0 || p.latW < p.cols || p.latH < p.rows ||
        p.latW > FALD_LATTICE_MAX_PX || p.latH > FALD_LATTICE_MAX_PX) return false;   // (the loader never produces these)
    unsigned long long right = (unsigned long long)p.originX + p.latW;
    unsigned long long bottom = (unsigned long long)p.originY + p.latH;
    return right <= (unsigned long long)width && bottom <= (unsigned long long)height;
}

uint32_t FaldZoneEdge(uint32_t index, uint32_t count, uint32_t extent) {
    if (count == 0) return 0;
    // ceil(index * extent / count - 1/2) = floor((2 index extent + count - 1) / (2 count)); 64-bit here, < 2^32 on the GPU
    return (uint32_t)((2ull * index * extent + count - 1ull) / (2ull * count));
}

uint32_t FaldZoneOfPixel(uint32_t local, uint32_t count, uint32_t extent) {
    if (extent == 0) return 0;
    // floor((local + 1/2) * count / extent): the zone the pixel's centre lies in
    return (uint32_t)(((2ull * local + 1ull) * count) / (2ull * extent));
}

bool FaldLatticeFractional(const FaldPanelParams& p) {
    return p.cols != 0 && p.rows != 0 && (p.latW % p.cols != 0 || p.latH % p.rows != 0);
}

bool FaldPanelFileStamp(const std::wstring& path, unsigned long long& size, unsigned long long& mtime) {
    WIN32_FILE_ATTRIBUTE_DATA fad;
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &fad)) return false;
    size = ((unsigned long long)fad.nFileSizeHigh << 32) | fad.nFileSizeLow;
    mtime = ((unsigned long long)fad.ftLastWriteTime.dwHighDateTime << 32) | fad.ftLastWriteTime.dwLowDateTime;
    return true;
}
