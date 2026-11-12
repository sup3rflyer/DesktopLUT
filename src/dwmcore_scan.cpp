// DesktopLUT - dwmcore_scan.cpp
// DwmHook.dll's dwmcore pattern scan, on a file read as data (dwmcore_scan.h). The loops below are copies of
// DllMain's (dwm_hook/dllmain.cpp, DLL_PROCESS_ATTACH): keep them in step.

#include "dwmcore_scan.h"
#include "../shared/dwm_hook_patterns.h"

#include <windows.h>
#include <cstring>

namespace {

constexpr size_t kMaxImage = 256u * 1024u * 1024u;   // a sanity bound, far above any dwmcore

template <typename T>
bool ReadAt(const std::vector<unsigned char>& f, size_t off, T& out) {
    if (off > f.size() || f.size() - off < sizeof(T)) return false;
    memcpy(&out, f.data() + off, sizeof(T));
    return true;
}

// DllMain's aob_match_inverse: true = MISMATCH ('?' in the pattern matches anything).
bool AobMismatch(const unsigned char* buf, const unsigned char* pat, size_t len) {
    for (size_t i = 0; i < len; ++i)
        if (buf[i] != pat[i] && pat[i] != '?') return true;
    return false;
}

int32_t ReadI32(const std::vector<unsigned char>& img, size_t off) {
    int32_t v = 0;
    memcpy(&v, img.data() + off, sizeof(v));
    return v;
}

// The file's version: VS_FIXEDFILEINFO follows the key L"VS_VERSION_INFO" (padded to 32 bits) in the resource.
DwmcoreVersion FindFixedFileVersion(const std::vector<unsigned char>& img) {
    DwmcoreVersion v;
    static const wchar_t kKey[] = L"VS_VERSION_INFO";
    const size_t keyBytes = sizeof(kKey);
    for (size_t i = 0; i + keyBytes + 8 + sizeof(VS_FIXEDFILEINFO) <= img.size(); i += 2) {
        if (memcmp(img.data() + i, kKey, keyBytes) != 0) continue;
        size_t off = (i + keyBytes + 3) & ~(size_t)3;
        VS_FIXEDFILEINFO ffi;
        if (off + sizeof(ffi) > img.size()) break;
        memcpy(&ffi, img.data() + off, sizeof(ffi));
        if (ffi.dwSignature != 0xFEEF04BD) continue;
        v.major = HIWORD(ffi.dwFileVersionMS);
        v.minor = LOWORD(ffi.dwFileVersionMS);
        v.build = HIWORD(ffi.dwFileVersionLS);
        v.rev = LOWORD(ffi.dwFileVersionLS);
        v.valid = true;
        break;
    }
    return v;
}

// --- DllMain's registry-global derivation (DeriveRegistryGlobalInner / ResolveDisableIndependentFlip), on RVAs ---

const size_t kRegistryStoreWindow = 0x80;   // dllmain.cpp kRegistryStoreWindow

bool FindBytes(const std::vector<unsigned char>& img, const unsigned char* pat, size_t len, size_t& at) {
    if (len == 0 || img.size() < len) return false;
    for (size_t i = 0; i + len <= img.size(); i++) {
        if (img[i] != pat[0]) continue;
        if (memcmp(img.data() + i, pat, len) == 0) { at = i; return true; }
    }
    return false;
}

bool DeriveRegistryGlobal(const std::vector<unsigned char>& img, const wchar_t* name, size_t nameBytes, size_t& out) {
    size_t str = 0;
    if (!FindBytes(img, reinterpret_cast<const unsigned char*>(name), nameBytes, str)) return false;
    const size_t n = img.size();
    for (size_t i = 0; i + 7 <= n; i++) {
        const unsigned char* p = img.data() + i;
        if ((p[0] != 0x48 && p[0] != 0x4C) || p[1] != 0x8D || (p[2] & 0xC7) != 0x05) continue;
        if ((int64_t)i + 7 + ReadI32(img, i + 3) != (int64_t)str) continue;
        size_t q = i + 7;
        size_t lim = q + kRegistryStoreWindow;
        if (lim > n - 6) lim = n - 6;
        for (; q < lim; q++) {
            if (img[q] != 0x89 || (img[q + 1] & 0xC7) != 0x05) continue;
            const int64_t tgt = (int64_t)q + 6 + ReadI32(img, q + 2);
            if (tgt >= 0 && (uint64_t)tgt + sizeof(int) <= n) { out = (size_t)tgt; return true; }
            break;
        }
    }
    return false;
}

void ResolveDisableIndependentFlip(const std::vector<unsigned char>& img, size_t otm, DwmcoreScan& s) {
    static const wchar_t kOtmName[] = L"OverlayTestMode";
    static const wchar_t kDifName[] = L"DisableIndependentFlip";
    size_t otmByName = 0;
    if (!DeriveRegistryGlobal(img, kOtmName, sizeof(kOtmName), otmByName) || otmByName != otm) {
        s.disableIndependentFlipWhy = "name-derivation cross-check failed";
        return;
    }
    size_t dif = 0;
    if (!DeriveRegistryGlobal(img, kDifName, sizeof(kDifName), dif)) {
        s.disableIndependentFlipWhy = "value name not found";
        return;
    }
    const int64_t delta = (int64_t)dif - (int64_t)otm;
    const int64_t absDelta = delta < 0 ? -delta : delta;
    if (absDelta > 0x200 || (dif & 3) != 0) {   // the module base is 64 KB aligned: RVA alignment = address alignment
        s.disableIndependentFlipWhy = "implausible location";
        return;
    }
    const int32_t cur = ReadI32(img, dif);   // the file's initial value (DllMain reads the live registry value)
    if (cur != 0 && cur != 1) {
        s.disableIndependentFlipWhy = "non-boolean value";
        return;
    }
    s.disableIndependentFlipResolved = true;
    s.disableIndependentFlipRva = (uint32_t)dif;
}

void Found(DwmcoreScan& s, DwmPattern p, size_t rva) {
    DwmPatternHit& h = s.hits[(int)p];
    h.found = true;
    h.rva = (uint32_t)rva;
}

void MarkScanned(DwmcoreScan& s, std::initializer_list<DwmPattern> ps) {
    for (DwmPattern p : ps) s.hits[(int)p].scanned = true;
}

// 25H2: OverlaysEnabled's `cmp dword ptr [rip+disp32], 5` operand is OverlayTestMode (DllMain: rip_offset at +2,
// instruction length 7).
void ResolveOverlayTestMode(const std::vector<unsigned char>& img, size_t oe, DwmcoreScan& s) {
    const int64_t cand = (int64_t)oe + 7 + ReadI32(img, oe + 2);
    if (cand >= 0 && (uint64_t)cand < img.size()) {
        s.overlayTestModeResolved = true;
        s.overlayTestModeRva = (uint32_t)cand;
        ResolveDisableIndependentFlip(img, (size_t)cand, s);
    }
}

// --- The branch loops: copies of DllMain's ---

void Scan25h2(const std::vector<unsigned char>& img, DwmcoreScan& s) {
    using P = DwmPattern;
    MarkScanned(s, { P::Present, P::IsCandidateDirectFlipCompatible, P::OverlaysEnabled,
                     P::CWindowContext_IsCandidateDirectFlipCompatible, P::CCompSwapChain_IsCandidateDirectFlipCompatible,
                     P::CCompVisual_IsCandidateForPromotion, P::COverlayContext_IsDirectFlipSupportedOnTarget,
                     P::CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible });
    const size_t size = img.size();
    if (size < sizeof COverlayContext_OverlaysEnabled_bytes_w11_25h2) return;
    auto miss = [&](P p) { return !s.Hit(p).found; };
    for (size_t i = 0; i <= size - sizeof COverlayContext_OverlaysEnabled_bytes_w11_25h2; i++) {
        const unsigned char* a = img.data() + i;
        const size_t left = size - i;
        if (miss(P::Present) && sizeof COverlayContext_Present_bytes_w11_25h2 <= left &&
            !AobMismatch(a, COverlayContext_Present_bytes_w11_25h2, sizeof COverlayContext_Present_bytes_w11_25h2))
            Found(s, P::Present, i);
        else if (miss(P::IsCandidateDirectFlipCompatible) &&
                 sizeof COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_25h2 <= left &&
                 !AobMismatch(a, COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_25h2,
                              sizeof COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_25h2))
            Found(s, P::IsCandidateDirectFlipCompatible, i);
        else if (miss(P::CWindowContext_IsCandidateDirectFlipCompatible) &&
                 sizeof CWindowContext_IsCandidateDirectFlipCompatible_bytes_w11_25h2 <= left &&
                 !AobMismatch(a, CWindowContext_IsCandidateDirectFlipCompatible_bytes_w11_25h2,
                              sizeof CWindowContext_IsCandidateDirectFlipCompatible_bytes_w11_25h2))
            Found(s, P::CWindowContext_IsCandidateDirectFlipCompatible, i);
        else if (miss(P::CCompSwapChain_IsCandidateDirectFlipCompatible) &&
                 sizeof CCompSwapChain_IsCandidateDirectFlipCompatible_bytes_w11_25h2 <= left &&
                 !AobMismatch(a, CCompSwapChain_IsCandidateDirectFlipCompatible_bytes_w11_25h2,
                              sizeof CCompSwapChain_IsCandidateDirectFlipCompatible_bytes_w11_25h2))
            Found(s, P::CCompSwapChain_IsCandidateDirectFlipCompatible, i);
        else if (miss(P::CCompVisual_IsCandidateForPromotion) &&
                 sizeof CCompVisual_IsCandidateForPromotion_bytes_w11_25h2 <= left &&
                 !AobMismatch(a, CCompVisual_IsCandidateForPromotion_bytes_w11_25h2,
                              sizeof CCompVisual_IsCandidateForPromotion_bytes_w11_25h2))
            Found(s, P::CCompVisual_IsCandidateForPromotion, i);
        else if (miss(P::COverlayContext_IsDirectFlipSupportedOnTarget) &&
                 sizeof COverlayContext_IsDirectFlipSupportedOnTarget_bytes_w11_25h2 <= left &&
                 !AobMismatch(a, COverlayContext_IsDirectFlipSupportedOnTarget_bytes_w11_25h2,
                              sizeof COverlayContext_IsDirectFlipSupportedOnTarget_bytes_w11_25h2))
            Found(s, P::COverlayContext_IsDirectFlipSupportedOnTarget, i);
        else if (miss(P::CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible) &&
                 sizeof CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_bytes_w11_25h2 <= left &&
                 !AobMismatch(a, CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_bytes_w11_25h2,
                              sizeof CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_bytes_w11_25h2))
            Found(s, P::CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible, i);
        else if (miss(P::OverlaysEnabled) && sizeof COverlayContext_OverlaysEnabled_bytes_w11_25h2 <= left &&
                 !AobMismatch(a, COverlayContext_OverlaysEnabled_bytes_w11_25h2,
                              sizeof COverlayContext_OverlaysEnabled_bytes_w11_25h2)) {
            Found(s, P::OverlaysEnabled, i);
            ResolveOverlayTestMode(img, i, s);
        }
        if (!miss(P::Present) && !miss(P::IsCandidateDirectFlipCompatible) && !miss(P::OverlaysEnabled)) break;
    }
    // DllMain's dedicated OverlaysEnabled pass (the same bytes as the pattern, wildcards 2..5).
    if (miss(P::OverlaysEnabled) && size >= 17) {
        for (size_t j = 0; j < size - 17; j++) {
            const unsigned char* c = img.data() + j;
            if (c[0] == 0x83 && c[1] == 0x3D && c[6] == 0x05 && c[7] == 0x74 && c[8] == 0x09 && c[9] == 0x83 &&
                c[10] == 0x79 && c[11] == 0x28 && c[12] == 0x01 && c[13] == 0x0F && c[14] == 0x97 && c[15] == 0xC0 &&
                c[16] == 0xC3) {
                Found(s, P::OverlaysEnabled, j);
                s.overlaysEnabledByDedicatedScan = true;
                ResolveOverlayTestMode(img, j, s);
                break;
            }
        }
    }
}

void Scan24h2(const std::vector<unsigned char>& img, DwmcoreScan& s) {
    using P = DwmPattern;
    MarkScanned(s, { P::Present, P::IsCandidateDirectFlipCompatible, P::OverlaysEnabled });
    const size_t size = img.size();
    if (size < sizeof COverlayContext_OverlaysEnabled_bytes_relative_w11_24h2) return;
    auto miss = [&](P p) { return !s.Hit(p).found; };
    for (size_t i = 0; i <= size - sizeof COverlayContext_OverlaysEnabled_bytes_relative_w11_24h2; i++) {
        const unsigned char* a = img.data() + i;
        const size_t left = size - i;
        if (miss(P::Present) && sizeof COverlayContext_Present_bytes_w11_24h2 <= left &&
            !AobMismatch(a, COverlayContext_Present_bytes_w11_24h2, sizeof COverlayContext_Present_bytes_w11_24h2))
            Found(s, P::Present, i);
        else if (miss(P::IsCandidateDirectFlipCompatible) &&
                 sizeof COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_24h2 <= left &&
                 !AobMismatch(a, COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_24h2,
                              sizeof COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_24h2))
            Found(s, P::IsCandidateDirectFlipCompatible, i);
        else if (miss(P::OverlaysEnabled) && sizeof COverlayContext_OverlaysEnabled_bytes_relative_w11_24h2 <= left &&
                 !AobMismatch(a, COverlayContext_OverlaysEnabled_bytes_relative_w11_24h2,
                              sizeof COverlayContext_OverlaysEnabled_bytes_relative_w11_24h2)) {
            // get_relative_address(address, 1, 5): the E8 call's target. DllMain does not range-check it; record it
            // as found either way (that is what DllMain hooks), the RVA may lie outside the image.
            const int64_t tgt = (int64_t)i + 5 + ReadI32(img, i + 1);
            Found(s, P::OverlaysEnabled, (size_t)(uint32_t)tgt);
        }
        if (!miss(P::Present) && !miss(P::IsCandidateDirectFlipCompatible) && !miss(P::OverlaysEnabled)) break;
    }
}

void ScanWin11(const std::vector<unsigned char>& img, DwmcoreScan& s) {
    using P = DwmPattern;
    MarkScanned(s, { P::Present, P::IsCandidateDirectFlipCompatible, P::OverlaysEnabled });
    const size_t size = img.size();
    if (size < sizeof COverlayContext_OverlaysEnabled_bytes_w11) return;
    auto miss = [&](P p) { return !s.Hit(p).found; };
    for (size_t i = 0; i <= size - sizeof COverlayContext_OverlaysEnabled_bytes_w11; i++) {
        const unsigned char* a = img.data() + i;
        const size_t left = size - i;
        if (miss(P::Present) && sizeof COverlayContext_Present_bytes_w11 <= left &&
            !AobMismatch(a, COverlayContext_Present_bytes_w11, sizeof COverlayContext_Present_bytes_w11))
            Found(s, P::Present, i);
        else if (miss(P::IsCandidateDirectFlipCompatible) &&
                 sizeof COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11 <= left &&
                 !AobMismatch(a, COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11,
                              sizeof COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11))
            Found(s, P::IsCandidateDirectFlipCompatible, i);
        else if (miss(P::OverlaysEnabled) && sizeof COverlayContext_OverlaysEnabled_bytes_w11 <= left &&
                 !AobMismatch(a, COverlayContext_OverlaysEnabled_bytes_w11, sizeof COverlayContext_OverlaysEnabled_bytes_w11))
            Found(s, P::OverlaysEnabled, i);
        if (!miss(P::Present) && !miss(P::IsCandidateDirectFlipCompatible) && !miss(P::OverlaysEnabled)) break;
    }
}

void ScanWin10(const std::vector<unsigned char>& img, DwmcoreScan& s) {
    using P = DwmPattern;
    MarkScanned(s, { P::Present, P::IsCandidateDirectFlipCompatible, P::OverlaysEnabled });
    const size_t size = img.size();
    if (size < sizeof(COverlayContext_Present_bytes)) return;
    auto miss = [&](P p) { return !s.Hit(p).found; };
    int found = 0;   // DllMain: a function-static counter (first injection into a dwm.exe)
    for (size_t i = 0; i <= size - sizeof(COverlayContext_Present_bytes); i++) {
        const unsigned char* a = img.data() + i;
        // DllMain compares with memcmp near the image end without a length guard; stay inside the buffer here.
        const size_t left = size - i;
        if (miss(P::Present) && sizeof(COverlayContext_Present_bytes) <= left &&
            !memcmp(a, COverlayContext_Present_bytes, sizeof(COverlayContext_Present_bytes)))
            Found(s, P::Present, i);
        else if (miss(P::IsCandidateDirectFlipCompatible) &&
                 sizeof(COverlayContext_IsCandidateDirectFlipCompatbile_bytes) <= left &&
                 !memcmp(a, COverlayContext_IsCandidateDirectFlipCompatbile_bytes,
                         sizeof(COverlayContext_IsCandidateDirectFlipCompatbile_bytes))) {
            if (++found == 2) Found(s, P::IsCandidateDirectFlipCompatible, i - 0xa);
        } else if (miss(P::OverlaysEnabled) && sizeof(COverlayContext_OverlaysEnabled_bytes) <= left &&
                   !memcmp(a, COverlayContext_OverlaysEnabled_bytes, sizeof(COverlayContext_OverlaysEnabled_bytes)))
            Found(s, P::OverlaysEnabled, i - 0x7);
        if (!miss(P::Present) && !miss(P::IsCandidateDirectFlipCompatible) && !miss(P::OverlaysEnabled)) break;
    }
}

}  // namespace

const char* DwmPatternName(DwmPattern p) {
    switch (p) {
    case DwmPattern::Present: return "COverlayContext::Present";
    case DwmPattern::IsCandidateDirectFlipCompatible: return "COverlayContext::IsCandidateDirectFlipCompatible";
    case DwmPattern::OverlaysEnabled: return "COverlayContext::OverlaysEnabled";
    case DwmPattern::CWindowContext_IsCandidateDirectFlipCompatible: return "CWindowContext::IsCandidateDirectFlipCompatible";
    case DwmPattern::CCompSwapChain_IsCandidateDirectFlipCompatible: return "CCompSwapChain::IsCandidateDirectFlipCompatible";
    case DwmPattern::CCompVisual_IsCandidateForPromotion: return "CCompVisual::IsCandidateForPromotion";
    case DwmPattern::COverlayContext_IsDirectFlipSupportedOnTarget: return "COverlayContext::IsDirectFlipSupportedOnTarget";
    case DwmPattern::CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible:
        return "CGlobalCompositionSurfaceInfo::IsAdvancedDirectFlipCompatible";
    case DwmPattern::Count: break;
    }
    return "?";
}

bool LoadDwmcoreImage(const std::wstring& path, DwmcoreImage& out) {
    out = DwmcoreImage();
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) { out.error = "cannot open the file"; return false; }
    LARGE_INTEGER sz = {};
    std::vector<unsigned char> file;
    bool ok = GetFileSizeEx(h, &sz) && sz.QuadPart > 0 && (uint64_t)sz.QuadPart <= kMaxImage;
    if (ok) {
        file.resize((size_t)sz.QuadPart);
        DWORD got = 0;
        size_t done = 0;
        while (ok && done < file.size()) {
            const DWORD want = (DWORD)(std::min<size_t>)(file.size() - done, 1u << 24);
            ok = ReadFile(h, file.data() + done, want, &got, nullptr) && got > 0;
            done += got;
        }
    }
    CloseHandle(h);
    if (!ok) { out.error = "cannot read the file"; return false; }

    IMAGE_DOS_HEADER dos;
    if (!ReadAt(file, 0, dos) || dos.e_magic != IMAGE_DOS_SIGNATURE || dos.e_lfanew <= 0) {
        out.error = "not a PE file"; return false;
    }
    const size_t nt = (size_t)dos.e_lfanew;
    DWORD sig = 0;
    IMAGE_FILE_HEADER fh;
    if (!ReadAt(file, nt, sig) || sig != IMAGE_NT_SIGNATURE || !ReadAt(file, nt + 4, fh)) {
        out.error = "not a PE file"; return false;
    }
    const size_t opt = nt + 4 + sizeof(IMAGE_FILE_HEADER);
    IMAGE_OPTIONAL_HEADER64 oh;
    if (fh.SizeOfOptionalHeader < sizeof(oh) || !ReadAt(file, opt, oh) || oh.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) {
        out.error = "not a 64-bit PE image"; return false;
    }
    if (oh.SizeOfImage == 0 || oh.SizeOfImage > kMaxImage || oh.SizeOfHeaders > oh.SizeOfImage) {
        out.error = "implausible image size"; return false;
    }
    out.bytes.assign(oh.SizeOfImage, 0);
    memcpy(out.bytes.data(), file.data(), (std::min<size_t>)(oh.SizeOfHeaders, file.size()));
    const size_t secs = opt + fh.SizeOfOptionalHeader;
    for (WORD k = 0; k < fh.NumberOfSections; ++k) {
        IMAGE_SECTION_HEADER sh;
        if (!ReadAt(file, secs + (size_t)k * sizeof(sh), sh)) { out.error = "truncated section table"; return false; }
        size_t n = sh.SizeOfRawData;
        if (sh.Misc.VirtualSize != 0 && sh.Misc.VirtualSize < n) n = sh.Misc.VirtualSize;
        if (sh.PointerToRawData > file.size()) continue;
        n = (std::min<size_t>)(n, file.size() - sh.PointerToRawData);
        if (sh.VirtualAddress >= out.bytes.size()) continue;
        n = (std::min<size_t>)(n, out.bytes.size() - sh.VirtualAddress);
        memcpy(out.bytes.data() + sh.VirtualAddress, file.data() + sh.PointerToRawData, n);
    }
    out.imageBase = oh.ImageBase;
    out.version = FindFixedFileVersion(out.bytes);
    out.valid = true;
    return true;
}

DwmcoreScan ScanDwmcoreBranch(const DwmcoreImage& img, DwmHookBranch branch) {
    DwmcoreScan s;
    s.branch = branch;
    if (!img.valid) return s;
    switch (branch) {
    case DwmHookBranch::Win11_25H2: Scan25h2(img.bytes, s); break;
    case DwmHookBranch::Win11_24H2: Scan24h2(img.bytes, s); break;
    case DwmHookBranch::Win11:      ScanWin11(img.bytes, s); break;
    case DwmHookBranch::Win10:      ScanWin10(img.bytes, s); break;
    }
    return s;
}

// A copy of DllMain's Dwmcore25h2AnchorsResolve (the 24H2-build path choice): both patterns by aob, first match
// each, OverlaysEnabled's OverlayTestMode operand inside the image.
bool Dwmcore25h2AnchorsResolve(const DwmcoreImage& img) {
    if (!img.valid) return false;
    const std::vector<unsigned char>& b = img.bytes;
    const size_t size = b.size();
    bool present = false, overlays = false;
    for (size_t i = 0; i < size && !(present && overlays); i++) {
        const unsigned char* a = b.data() + i;
        const size_t left = size - i;
        if (!present && sizeof COverlayContext_Present_bytes_w11_25h2 <= left &&
            !AobMismatch(a, COverlayContext_Present_bytes_w11_25h2, sizeof COverlayContext_Present_bytes_w11_25h2))
            present = true;
        if (!overlays && sizeof COverlayContext_OverlaysEnabled_bytes_w11_25h2 <= left &&
            !AobMismatch(a, COverlayContext_OverlaysEnabled_bytes_w11_25h2, sizeof COverlayContext_OverlaysEnabled_bytes_w11_25h2)) {
            const int64_t otm = (int64_t)i + 7 + ReadI32(b, i + 2);
            if (otm >= 0 && (uint64_t)otm < size) overlays = true;
        }
    }
    return present && overlays;
}

DwmHookBranch DwmHookBranchFor(uint32_t osBuild, const DwmcoreImage* img) {
    const DwmHookBranch byOs = DwmHookBranchForOsBuild(osBuild);
    if (byOs == DwmHookBranch::Win11_24H2 && img && img->valid && Dwmcore25h2AnchorsResolve(*img))
        return DwmHookBranch::Win11_25H2;
    return byOs;
}

DwmHookPreflight PreflightDwmcore(uint32_t osBuild, const DwmcoreImage& img) {
    DwmHookPreflight p;
    p.branch = DwmHookBranchFor(osBuild, &img);
    if (!img.valid) {   // nothing to judge by: the DLL decides, as before
        p.error = img.error.empty() ? "no image" : img.error;
        return p;
    }
    p.ran = true;
    const DwmcoreScan s = ScanDwmcoreBranch(img, p.branch);
    auto need = [&](DwmPattern pat) { if (!s.Hit(pat).found) p.missing.push_back(pat); };
    need(DwmPattern::Present);
    need(DwmPattern::OverlaysEnabled);
    if (p.branch == DwmHookBranch::Win11_25H2) {
        // OverlaysEnabled found but its OverlayTestMode operand outside the image: DllMain patches nothing of it.
        if (s.Hit(DwmPattern::OverlaysEnabled).found && !s.overlayTestModeResolved)
            p.missing.push_back(DwmPattern::OverlaysEnabled);
    } else {
        need(DwmPattern::IsCandidateDirectFlipCompatible);
        // 24H2: the call target DllMain hooks must lie inside the image.
        const DwmPatternHit& oe = s.Hit(DwmPattern::OverlaysEnabled);
        if (oe.found && oe.rva >= img.bytes.size()) p.missing.push_back(DwmPattern::OverlaysEnabled);
    }
    p.ok = p.missing.empty();
    return p;
}

const DwmHookPreflight& DwmHookPreflightCheck() {
    static const DwmHookPreflight s = [] {
        LARGE_INTEGER f = {}, t0 = {}, t1 = {};
        QueryPerformanceFrequency(&f);
        QueryPerformanceCounter(&t0);
        DwmHookPreflight p;
        wchar_t sys[MAX_PATH] = {};
        const UINT n = GetSystemDirectoryW(sys, MAX_PATH);
        DwmcoreImage img;
        if (n > 0 && n < MAX_PATH) LoadDwmcoreImage(std::wstring(sys) + L"\\dwmcore.dll", img);
        p = PreflightDwmcore(DwmHookBuildCheck().osBuild, img);
        QueryPerformanceCounter(&t1);
        p.scanMs = f.QuadPart ? double(t1.QuadPart - t0.QuadPart) * 1000.0 / double(f.QuadPart) : 0.0;
        return p;
    }();
    return s;
}

std::wstring DwmHookPreflightReason(const DwmHookPreflight& p) {
    if (p.ok) return {};
    std::wstring r = L"This Windows update moved what the DWM hook needs in the desktop compositor (dwmcore.dll), so "
                     L"DesktopLUT did not inject it. Not found:";
    for (DwmPattern m : p.missing) {
        const char* name = DwmPatternName(m);
        r += L"\n  ";
        while (*name) r += (wchar_t)(unsigned char)*name++;
    }
    return r;
}
