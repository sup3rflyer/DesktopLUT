#include "ui_model.h"

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
