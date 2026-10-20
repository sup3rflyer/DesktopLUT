#include "ui_model.h"

#include <cmath>
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

Light CombineLights(Light a, Light b) {
    auto rank = [](Light l) {
        switch (l) {
        case Light::Fault:      return 4;
        case Light::Configured:
        case Light::Verified:   return 3;
        case Light::Pending:
        case Light::Off:        return 2;
        default:                return 1;
        }
    };
    return rank(a) >= rank(b) ? a : b;
}

bool MhcPrimariesContent(const MHCSettings& mhc) {
    const bool d65 = std::fabs(mhc.whiteBalanceWx - 0.3127f) < 0.001f && std::fabs(mhc.whiteBalanceWy - 0.3290f) < 0.001f;
    const bool wb = mhc.whiteBalanceEnabled && mhc.whiteBalanceWy > 0.001f && !d65;
    return mhc.primariesEnabled || wb;
}

bool MhcGrayscaleContent(const MHCSettings& mhc, bool hdr) {
    // An imported file carries a curve when it is a 1D cube, or an SDR ICC profile's TRC (HDR ICC curves
    // are not used: only the primaries come from them).
    const bool imported = !mhc.sourceFilePath.empty() && (mhc.sourceIs1DCube || (!hdr && mhc.hasPerChannelTRC));
    const bool base = mhc.sourceFilePath.empty() && mhc.baseGrayscale.enabled && !mhc.baseGrayscale.points.empty();
    const bool correction = mhc.correctionGrayscale.enabled && !mhc.correctionGrayscale.points.empty();
    return imported || base || correction || (hdr && mhc.desktopGammaEnabled);
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

std::vector<Stage> BuildPixelPath(const MonitorSettings& ms, Mode shown, Mode live, const PathRuntime& rt) {
    const bool hdr = PipelineIsHdr(shown);
    const ColorCorrectionSettings& cc = hdr ? ms.hdrColorCorrection : ms.sdrColorCorrection;
    const MHCSettings& mhc = hdr ? ms.hdrMHC : ms.sdrMHC;
    const std::wstring& lut = hdr ? ms.hdrPath : ms.sdrPath;
    // Faults only for the mode the display is in (the other side shows what would run there).
    const bool liveSide = live != Mode::Unknown && PipelineIsHdr(live) == hdr;
    // Tone mapping and the 3D LUT are drawn by the hook in hook mode (it stays resident while a dead overlay
    // thread waits for its auto-restart), else by the overlay; FALD runs in the overlay engine.
    const bool cubeEngineDown = rt.hookMode ? !rt.hookActive : !rt.running;
    const bool faldEngineDown = !rt.running || (rt.hookMode && !rt.hookActive);

    // A live layer (tone map, 3D LUT, FALD): nothing set / switched off or suspended / runs / should run.
    auto liveLayer = [&](bool content, bool on, bool cannotRun, bool engineDown) {
        if (!content) return Light::None;
        if (!on || rt.suspended) return Light::Off;
        if (liveSide && (engineDown || cannotRun)) return Light::Fault;
        return Light::Configured;
    };
    // An MHC stage: nothing to do or no MHC / bypassed / in the installed profile / enabled but not installed.
    const bool mhcInstalled = mhc.enabled && !mhc.profileName.empty();
    auto mhcStage = [&](bool content, bool bypassed) {
        if (!content || !mhc.enabled) return Light::None;
        if (bypassed) return Light::Off;
        return mhcInstalled ? Light::Configured : Light::Fault;
    };
    // Dimmed (a dashed node) = not changing the picture.
    auto stage = [](StageId id, Light l) { return Stage{ id, l, true, l == Light::None || l == Light::Off }; };

    std::vector<Stage> path;
    path.push_back({ StageId::Desktop, Light::NotApplicable, false, false });

    // Tone mapping runs before the 3D LUT in the hook (and overlay) shader, HDR only. Its switch is all it
    // has: off is nothing set.
    // A live edit holds it off (switched off, a ring) while it runs.
    if (hdr) path.push_back(stage(StageId::ToneMap, liveLayer(cc.tonemap.enabled, !rt.editHeld, false, cubeEngineDown)));

    path.push_back(stage(StageId::Lut3D,
                         liveLayer(!lut.empty(), !(hdr ? ms.hdrLutBypass : ms.sdrLutBypass), false, cubeEngineDown)));

    // FALD needs a panel file to exist at all; it runs only on FP16 back buffers (HDR or ACM SDR). Without one the
    // path may offer it in the same place (same label, a "+" where the light goes: nothing moves when it is added).
    if (!cc.fald.paramsPath.empty()) {
        const bool fp16 = live == Mode::HDR || live == Mode::AcmSdr || live == Mode::Unknown;
        path.push_back(stage(StageId::Fald, liveLayer(true, cc.fald.enabled && !rt.editHeld, !fp16, faldEngineDown)));
    } else if (rt.offerFald && (hdr || live != Mode::SDR)) {
        Stage add = stage(StageId::Fald, Light::None);
        add.add = true;
        path.push_back(add);
    }

    // The overlay shader tweak (pipe / INI only, no classic control) — shown so a live correction is
    // never invisible.
    if (ShaderTweakActive(cc)) path.push_back({ StageId::ShaderTweak, Light::Configured, true, false });

    path.push_back({ StageId::Scanout, Light::NotApplicable, false, false });

    path.push_back(stage(StageId::Primaries, mhcStage(MhcPrimariesContent(mhc), mhc.bypassPrimaries)));
    path.push_back(stage(StageId::Grayscale, mhcStage(MhcGrayscaleContent(mhc, hdr), mhc.bypassGrayscale)));
    path.push_back({ StageId::Panel, Light::NotApplicable, false, false });
    return path;
}

}  // namespace ui
