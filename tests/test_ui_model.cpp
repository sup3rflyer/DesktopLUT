#include "doctest.h"
#include "ui/ui_model.h"
#include <algorithm>

// ============================================================================
// Interface view model (src/ui/ui_model.*): the pixel path a display's pipeline is drawn as.
// Pins the code facts the design had to bend to (docs/gui-rework-plan.md §0):
//  C3 FALD runs only on FP16 back buffers (HDR / ACM SDR) and only exists with a panel file;
//  C4 the overlay shader tweak is shown whenever it is active; C5 both MHC stages share one light.
// ============================================================================

using namespace ui;

namespace {

bool Has(const std::vector<Stage>& p, StageId id) {
    return std::any_of(p.begin(), p.end(), [&](const Stage& s) { return s.id == id; });
}

const Stage& Get(const std::vector<Stage>& p, StageId id) {
    return *std::find_if(p.begin(), p.end(), [&](const Stage& s) { return s.id == id; });
}

size_t IndexOf(const std::vector<Stage>& p, StageId id) {
    return (size_t)(std::find_if(p.begin(), p.end(), [&](const Stage& s) { return s.id == id; }) - p.begin());
}

}  // namespace

TEST_CASE("ui_model: SDR pipeline order, no tone map, markers not selectable") {
    MonitorSettings ms;
    ms.sdrPath = L"C:\\x\\sdr.cube";
    auto p = BuildPixelPath(ms, Mode::SDR, Mode::SDR);
    CHECK_FALSE(Has(p, StageId::ToneMap));
    CHECK_FALSE(Has(p, StageId::Fald));
    CHECK_FALSE(Has(p, StageId::ShaderTweak));
    REQUIRE(p.size() == 6);
    CHECK(p.front().id == StageId::Desktop);
    CHECK(p.back().id == StageId::Panel);
    CHECK(IndexOf(p, StageId::Lut3D) < IndexOf(p, StageId::Scanout));
    CHECK(IndexOf(p, StageId::Scanout) < IndexOf(p, StageId::Primaries));
    CHECK(IndexOf(p, StageId::Primaries) < IndexOf(p, StageId::Grayscale));
    CHECK_FALSE(Get(p, StageId::Desktop).selectable);
    CHECK_FALSE(Get(p, StageId::Scanout).selectable);
    CHECK(Get(p, StageId::Lut3D).light == Light::Configured);
}

TEST_CASE("ui_model: HDR pipeline has the tone map before the 3D LUT and reads the HDR side") {
    MonitorSettings ms;
    ms.sdrPath = L"sdr.cube";   // must not leak into the HDR pipeline
    ms.hdrColorCorrection.tonemap.enabled = true;
    auto p = BuildPixelPath(ms, Mode::HDR, Mode::HDR);
    REQUIRE(Has(p, StageId::ToneMap));
    CHECK(IndexOf(p, StageId::ToneMap) < IndexOf(p, StageId::Lut3D));
    CHECK(Get(p, StageId::ToneMap).light == Light::Configured);
    CHECK(Get(p, StageId::Lut3D).light == Light::Off);
}

TEST_CASE("ui_model: FALD exists only with a panel file and only runs on FP16 back buffers") {
    MonitorSettings ms;
    CHECK_FALSE(Has(BuildPixelPath(ms, Mode::AcmSdr, Mode::AcmSdr), StageId::Fald));

    ms.sdrColorCorrection.fald.paramsPath = L"panel.bin";
    ms.sdrColorCorrection.fald.enabled = true;
    // ACM SDR: FP16 composition, the layer runs.
    auto acm = BuildPixelPath(ms, Mode::AcmSdr, Mode::AcmSdr);
    REQUIRE(Has(acm, StageId::Fald));
    CHECK(Get(acm, StageId::Fald).light == Light::Configured);
    CHECK_FALSE(Get(acm, StageId::Fald).dimmed);
    // Legacy SDR: 8-bit composition, not applicable.
    auto legacy = BuildPixelPath(ms, Mode::SDR, Mode::SDR);
    REQUIRE(Has(legacy, StageId::Fald));
    CHECK(Get(legacy, StageId::Fald).light == Light::NotApplicable);
    CHECK(Get(legacy, StageId::Fald).dimmed);
    // File loaded but the layer switched off: shown dimmed.
    ms.sdrColorCorrection.fald.enabled = false;
    auto off = BuildPixelPath(ms, Mode::AcmSdr, Mode::AcmSdr);
    CHECK(Get(off, StageId::Fald).light == Light::Off);
    CHECK(Get(off, StageId::Fald).dimmed);
}

TEST_CASE("ui_model: the overlay shader tweak is a stage whenever it is active") {
    MonitorSettings ms;
    CHECK_FALSE(Has(BuildPixelPath(ms, Mode::SDR, Mode::SDR), StageId::ShaderTweak));
    ms.sdrColorCorrection.grayscale.enabled = true;
    auto p = BuildPixelPath(ms, Mode::SDR, Mode::SDR);
    REQUIRE(Has(p, StageId::ShaderTweak));
    CHECK(IndexOf(p, StageId::ShaderTweak) < IndexOf(p, StageId::Scanout));
    CHECK(ShaderTweakActive(ms.sdrColorCorrection));
    ColorCorrectionSettings cc;
    cc.grayscale.use24Gamma = true;
    CHECK(ShaderTweakActive(cc));
}

TEST_CASE("ui_model: both MHC stages share one light, configured only with an installed profile") {
    MonitorSettings ms;
    auto off = BuildPixelPath(ms, Mode::HDR, Mode::HDR);
    CHECK(Get(off, StageId::Primaries).light == Light::Off);
    ms.hdrMHC.enabled = true;   // enabled but never installed: no profile name
    CHECK(MhcLight(ms.hdrMHC) == Light::Off);
    ms.hdrMHC.profileName = L"DesktopLUT_Mon0_HDR_1.icm";
    auto on = BuildPixelPath(ms, Mode::HDR, Mode::HDR);
    CHECK(Get(on, StageId::Primaries).light == Light::Configured);
    CHECK(Get(on, StageId::Grayscale).light == Light::Configured);
}

TEST_CASE("ui_model: mode badges") {
    CHECK(std::string(ModeBadge(Mode::HDR)) == "HDR");
    CHECK(std::string(ModeBadge(Mode::SDR)) == "SDR");
    CHECK(std::string(ModeBadge(Mode::AcmSdr)).find("ACM") != std::string::npos);
    CHECK(PipelineIsHdr(Mode::HDR));
    CHECK_FALSE(PipelineIsHdr(Mode::AcmSdr));
}
