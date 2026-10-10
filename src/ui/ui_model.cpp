#include "ui_model.h"

#include <cwctype>

namespace ui {

const char* ModeBadge(Mode m) {
    switch (m) {
    case Mode::HDR:    return "HDR";
    case Mode::AcmSdr: return "SDR \xC2\xB7 ACM";
    case Mode::SDR:    return "SDR";
    default:           return "\xE2\x80\x94";
    }
}

const char* StageLabel(StageId id) {
    switch (id) {
    case StageId::Desktop:     return "DESKTOP";
    case StageId::ToneMap:     return "TONE MAP";
    case StageId::Lut3D:       return "3D LUT";
    case StageId::Fald:        return "FALD";
    case StageId::ShaderTweak: return "SHADER TWEAK";
    case StageId::Scanout:     return "SCANOUT";
    case StageId::Primaries:   return "PRIMARIES";
    case StageId::Grayscale:   return "GRAYSCALE";
    case StageId::Panel:       return "PANEL";
    }
    return "";
}

Light MhcLight(const MHCSettings& mhc) {
    return (mhc.enabled && !mhc.profileName.empty()) ? Light::Configured : Light::Off;
}

bool ShaderTweakActive(const ColorCorrectionSettings& cc) {
    return cc.primariesEnabled || cc.grayscale.enabled || cc.grayscale.use24Gamma;
}

namespace {

// PNP vendor ids of common display makers (EDID manufacturer id).
const wchar_t* VendorName(const std::wstring& pnp) {
    static const struct { const wchar_t* id; const wchar_t* name; } kVendors[] = {
        { L"ACI", L"ASUS" }, { L"ACR", L"Acer" }, { L"AOC", L"AOC" }, { L"APP", L"Apple" }, { L"AUO", L"AUO" },
        { L"AUS", L"ASUS" }, { L"BNQ", L"BenQ" }, { L"BOE", L"BOE" }, { L"CMN", L"Innolux" }, { L"DEL", L"Dell" },
        { L"EIZ", L"EIZO" }, { L"ENC", L"EIZO" }, { L"GBT", L"Gigabyte" }, { L"GSM", L"LG" }, { L"HIS", L"Hisense" },
        { L"HKC", L"HKC" }, { L"HPN", L"HP" }, { L"HWP", L"HP" }, { L"IVM", L"iiyama" }, { L"KTC", L"KTC" },
        { L"LEN", L"Lenovo" }, { L"LGD", L"LG" }, { L"MEI", L"Panasonic" }, { L"MSI", L"MSI" }, { L"NEC", L"NEC" },
        { L"PHL", L"Philips" }, { L"SAM", L"Samsung" }, { L"SDC", L"Samsung" }, { L"SEC", L"Samsung" },
        { L"SHP", L"Sharp" }, { L"SNY", L"Sony" }, { L"TCL", L"TCL" }, { L"VSC", L"ViewSonic" }, { L"XMI", L"Xiaomi" },
    };
    for (const auto& v : kVendors) if (pnp == v.id) return v.name;
    return nullptr;
}

// "AUS322A" from an EDID id ("AUS322A-335544320") or a device path ("\\?\DISPLAY#AUS322A#...").
std::wstring HardwareId(const DisplayIdentity& id) {
    if (!id.edidId.empty()) {
        size_t dash = id.edidId.find(L'-');
        return dash == std::wstring::npos ? id.edidId : id.edidId.substr(0, dash);
    }
    size_t a = id.devicePath.find(L'#');
    if (a == std::wstring::npos) return {};
    size_t b = id.devicePath.find(L'#', a + 1);
    return b == std::wstring::npos ? std::wstring() : id.devicePath.substr(a + 1, b - a - 1);
}

}  // namespace

std::wstring DisplayLabel(const DisplayIdentity& id, const std::wstring& osName) {
    if (!osName.empty()) return osName;
    if (!id.friendlyName.empty()) return id.friendlyName;
    std::wstring hw = HardwareId(id);
    if (hw.size() >= 4) {
        std::wstring pnp = hw.substr(0, 3), product = hw.substr(3);
        for (wchar_t& c : pnp) c = (wchar_t)towupper(c);
        const wchar_t* vendor = VendorName(pnp);
        return (vendor ? std::wstring(vendor) : pnp) + L" " + product;
    }
    return L"Display";
}

std::vector<Stage> BuildPixelPath(const MonitorSettings& ms, Mode shown, Mode live) {
    const bool hdr = PipelineIsHdr(shown);
    const ColorCorrectionSettings& cc = hdr ? ms.hdrColorCorrection : ms.sdrColorCorrection;
    const MHCSettings& mhc = hdr ? ms.hdrMHC : ms.sdrMHC;
    const std::wstring& lut = hdr ? ms.hdrPath : ms.sdrPath;

    std::vector<Stage> path;
    path.push_back({ StageId::Desktop, Light::NotApplicable, false, false });

    // Tone mapping runs before the 3D LUT in the hook (and overlay) shader, HDR only.
    if (hdr) path.push_back({ StageId::ToneMap, cc.tonemap.enabled ? Light::Configured : Light::Off, true, false });

    path.push_back({ StageId::Lut3D, lut.empty() ? Light::Off : Light::Configured, true, false });

    // FALD needs a panel file to exist at all; it runs only on FP16 back buffers (HDR or ACM SDR).
    if (!cc.fald.paramsPath.empty()) {
        const bool fp16 = (live == Mode::HDR || live == Mode::AcmSdr || live == Mode::Unknown);
        const bool liveMatchesSide = (live == Mode::Unknown) || (PipelineIsHdr(live) == hdr);
        Stage s{ StageId::Fald, Light::Off, true, false };
        if (!fp16 && liveMatchesSide) {
            s.light = Light::NotApplicable;   // legacy SDR: the layer cannot run
            s.dimmed = true;
        } else if (cc.fald.enabled) {
            s.light = Light::Configured;
        } else {
            s.dimmed = true;                  // file loaded, layer switched off
        }
        path.push_back(s);
    }

    // The overlay shader tweak (pipe / INI only, no classic control) — shown so a live correction is
    // never invisible.
    if (ShaderTweakActive(cc)) path.push_back({ StageId::ShaderTweak, Light::Configured, true, false });

    path.push_back({ StageId::Scanout, Light::NotApplicable, false, false });

    const Light m = MhcLight(mhc);
    path.push_back({ StageId::Primaries, m, true, false });
    path.push_back({ StageId::Grayscale, m, true, false });
    path.push_back({ StageId::Panel, Light::NotApplicable, false, false });
    return path;
}

}  // namespace ui
