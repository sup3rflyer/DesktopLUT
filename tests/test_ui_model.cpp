#include "doctest.h"
#include "ui/ui_model.h"
#include "ui/ui_widgets.h"
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

// The live layers running on the overlay (no faults from the engine).
PathRuntime Running() {
    PathRuntime rt;
    rt.running = true;
    return rt;
}

}  // namespace

TEST_CASE("ui_model: SDR pipeline order, no tone map, markers not selectable") {
    MonitorSettings ms;
    ms.sdrPath = L"C:\\x\\sdr.cube";
    auto p = BuildPixelPath(ms, Mode::SDR, Mode::SDR, Running());
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
    auto p = BuildPixelPath(ms, Mode::HDR, Mode::HDR, Running());
    REQUIRE(Has(p, StageId::ToneMap));
    CHECK(IndexOf(p, StageId::ToneMap) < IndexOf(p, StageId::Lut3D));
    CHECK(Get(p, StageId::ToneMap).light == Light::Configured);
    CHECK(Get(p, StageId::Lut3D).light == Light::None);    // no HDR cube: nothing set, no light
    CHECK(Get(p, StageId::Lut3D).dimmed);
    ms.hdrColorCorrection.tonemap.enabled = false;           // tone map's switch is all it has
    CHECK(Get(BuildPixelPath(ms, Mode::HDR, Mode::HDR, Running()), StageId::ToneMap).light == Light::None);
}

TEST_CASE("ui_model: FALD exists only with a panel file and only runs on FP16 back buffers") {
    MonitorSettings ms;
    CHECK_FALSE(Has(BuildPixelPath(ms, Mode::AcmSdr, Mode::AcmSdr), StageId::Fald));

    ms.sdrColorCorrection.fald.paramsPath = L"panel.bin";
    ms.sdrColorCorrection.fald.enabled = true;
    // ACM SDR: FP16 composition, the layer runs.
    auto acm = BuildPixelPath(ms, Mode::AcmSdr, Mode::AcmSdr, Running());
    REQUIRE(Has(acm, StageId::Fald));
    CHECK(Get(acm, StageId::Fald).light == Light::Configured);
    CHECK_FALSE(Get(acm, StageId::Fald).dimmed);
    // Legacy SDR: 8-bit composition: switched on, it cannot reach the screen.
    auto legacy = BuildPixelPath(ms, Mode::SDR, Mode::SDR, Running());
    REQUIRE(Has(legacy, StageId::Fald));
    CHECK(Get(legacy, StageId::Fald).light == Light::Fault);
    CHECK_FALSE(Get(legacy, StageId::Fald).dimmed);
    // File loaded but the layer switched off: a ring, dimmed.
    ms.sdrColorCorrection.fald.enabled = false;
    auto off = BuildPixelPath(ms, Mode::AcmSdr, Mode::AcmSdr, Running());
    CHECK(Get(off, StageId::Fald).light == Light::Off);
    CHECK(Get(off, StageId::Fald).dimmed);
}

TEST_CASE("ui_model: a live edit's hold shows tone mapping and FALD switched off (a ring)") {
    MonitorSettings ms;
    ms.hdrColorCorrection.tonemap.enabled = true;
    ms.hdrColorCorrection.fald.paramsPath = L"panel_hdr.bin";
    ms.hdrColorCorrection.fald.enabled = true;
    PathRuntime rt = Running();
    rt.editHeld = true;
    auto p = BuildPixelPath(ms, Mode::HDR, Mode::HDR, rt);
    CHECK(Get(p, StageId::ToneMap).light == Light::Off);
    CHECK(Get(p, StageId::ToneMap).dimmed);
    CHECK(Get(p, StageId::Fald).light == Light::Off);
    CHECK(Get(p, StageId::Fald).dimmed);
    rt.editHeld = false;
    p = BuildPixelPath(ms, Mode::HDR, Mode::HDR, rt);
    CHECK(Get(p, StageId::ToneMap).light == Light::Configured);
    CHECK(Get(p, StageId::Fald).light == Light::Configured);
    // Nothing set stays nothing (no light), held or not.
    MonitorSettings none;
    rt.editHeld = true;
    CHECK(Get(BuildPixelPath(none, Mode::HDR, Mode::HDR, rt), StageId::ToneMap).light == Light::None);
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

TEST_CASE("ui_model: each MHC stage lights for what it does in the installed profile") {
    MonitorSettings ms;
    auto none = BuildPixelPath(ms, Mode::HDR, Mode::HDR);
    CHECK(Get(none, StageId::Primaries).light == Light::None);
    CHECK(Get(none, StageId::Grayscale).light == Light::None);
    ms.hdrMHC.enabled = true;
    ms.hdrMHC.profileName = L"DesktopLUT_Mon0_HDR_1.icm";
    // An installed profile with nothing in a stage: still no light for it.
    auto empty = BuildPixelPath(ms, Mode::HDR, Mode::HDR);
    CHECK(Get(empty, StageId::Primaries).light == Light::None);
    ms.hdrMHC.primariesEnabled = true;
    ms.hdrMHC.desktopGammaEnabled = true;   // desktop gamma is grayscale-stage work (HDR)
    auto on = BuildPixelPath(ms, Mode::HDR, Mode::HDR);
    CHECK(Get(on, StageId::Primaries).light == Light::Configured);
    CHECK(Get(on, StageId::Grayscale).light == Light::Configured);
    // Switched off: a ring, dimmed; the other stage is untouched.
    ms.hdrMHC.bypassPrimaries = true;
    auto bypassed = BuildPixelPath(ms, Mode::HDR, Mode::HDR);
    CHECK(Get(bypassed, StageId::Primaries).light == Light::Off);
    CHECK(Get(bypassed, StageId::Primaries).dimmed);
    CHECK(Get(bypassed, StageId::Grayscale).light == Light::Configured);
    // Enabled with work to do but nothing installed: it should be on screen and is not.
    ms.hdrMHC.bypassPrimaries = false;
    ms.hdrMHC.profileName.clear();
    CHECK(Get(BuildPixelPath(ms, Mode::HDR, Mode::HDR), StageId::Primaries).light == Light::Fault);
    // MHC off (removed): nothing runs, no light.
    ms.hdrMHC.enabled = false;
    CHECK(Get(BuildPixelPath(ms, Mode::HDR, Mode::HDR), StageId::Primaries).light == Light::None);
}

TEST_CASE("ui_model: live layers follow Suspend and the engine; faults only for the mode the display is in") {
    MonitorSettings ms;
    ms.hdrPath = L"hdr.cube";
    ms.hdrColorCorrection.tonemap.enabled = true;
    ms.hdrMHC.enabled = true;
    ms.hdrMHC.profileName = L"p.icm";
    ms.hdrMHC.primariesEnabled = true;
    PathRuntime rt = Running();
    CHECK(Get(BuildPixelPath(ms, Mode::HDR, Mode::HDR, rt), StageId::Lut3D).light == Light::Configured);
    // Suspended: the live layers are rings; MHC does not depend on Suspend.
    rt.suspended = true;
    auto susp = BuildPixelPath(ms, Mode::HDR, Mode::HDR, rt);
    CHECK(Get(susp, StageId::Lut3D).light == Light::Off);
    CHECK(Get(susp, StageId::ToneMap).light == Light::Off);
    CHECK(Get(susp, StageId::Primaries).light == Light::Configured);
    // Hook mode with the hook not injected: should be applied, is not.
    rt = Running();
    rt.hookMode = true;
    rt.hookActive = false;
    CHECK(Get(BuildPixelPath(ms, Mode::HDR, Mode::HDR, rt), StageId::Lut3D).light == Light::Fault);
    // The same, viewed from the other mode (the display is in SDR): what would run, no fault.
    CHECK(Get(BuildPixelPath(ms, Mode::HDR, Mode::SDR, rt), StageId::Lut3D).light == Light::Configured);
    // Not running at all (and not suspended): a fault too.
    CHECK(Get(BuildPixelPath(ms, Mode::HDR, Mode::HDR, PathRuntime{}), StageId::ToneMap).light == Light::Fault);
    // The 3D LUT stage switched off: a ring, dimmed, whatever the engine does.
    ms.hdrLutBypass = true;
    auto bypassed = BuildPixelPath(ms, Mode::HDR, Mode::HDR, Running());
    CHECK(Get(bypassed, StageId::Lut3D).light == Light::Off);
    CHECK(Get(bypassed, StageId::Lut3D).dimmed);
}

TEST_CASE("ui_model: what counts as MHC stage work, and combining lights") {
    MHCSettings m;
    CHECK_FALSE(MhcPrimariesContent(m));
    m.whiteBalanceEnabled = true;                 // D65 target: the matrix is unchanged
    CHECK_FALSE(MhcPrimariesContent(m));
    m.whiteBalanceWx = 0.30f;
    CHECK(MhcPrimariesContent(m));
    MHCSettings g;
    CHECK_FALSE(MhcGrayscaleContent(g, true));
    g.sourceFilePath = L"panel.icc";              // an HDR ICC import gives primaries only
    CHECK_FALSE(MhcGrayscaleContent(g, true));
    g.hasPerChannelTRC = true;                    // an SDR ICC import carries its TRC
    CHECK(MhcGrayscaleContent(g, false));
    g.sourceIs1DCube = true;                      // a 1D cube is a curve in either mode
    CHECK(MhcGrayscaleContent(g, true));
    MHCSettings dg;
    dg.desktopGammaEnabled = true;
    CHECK(MhcGrayscaleContent(dg, true));
    CHECK_FALSE(MhcGrayscaleContent(dg, false));  // desktop gamma is HDR only
    CHECK(CombineLights(Light::None, Light::Off) == Light::Off);
    CHECK(CombineLights(Light::Off, Light::Configured) == Light::Configured);
    CHECK(CombineLights(Light::Configured, Light::Fault) == Light::Fault);
    CHECK(CombineLights(Light::None, Light::None) == Light::None);
}

TEST_CASE("ui_model: display labels fall back to the EDID maker and product code") {
    DisplayIdentity id;
    id.devicePath = L"\\\\?\\DISPLAY#AUS322A#5&14ca04b&2&UID4353#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}";
    id.edidId = L"AUS322A-335544320";
    CHECK(DisplayLabel(id, L"PA32UCXR") == L"PA32UCXR");        // the OS name wins when present
    CHECK(DisplayLabel(id, L"") == L"ASUS 322A");               // EDIDs (and CRU overrides) often carry none
    id.edidId.clear();
    CHECK(DisplayLabel(id, L"") == L"ASUS 322A");               // device path when there is no EDID id
    DisplayIdentity lg;
    lg.edidId = L"GSM84CD-16843009";
    CHECK(DisplayLabel(lg, L"") == L"LG 84CD");
    DisplayIdentity unknownVendor;
    unknownVendor.edidId = L"ZZZ1234";
    CHECK(DisplayLabel(unknownVendor, L"") == L"ZZZ 1234");
    CHECK(DisplayLabel(DisplayIdentity{}, L"") == L"Display");
    DisplayIdentity named;
    named.friendlyName = L"Kitchen TV";
    named.edidId = L"SAM0F00";
    CHECK(DisplayLabel(named, L"") == L"Kitchen TV");
}

TEST_CASE("ui_model: mode badges") {
    CHECK(std::string(ModeBadge(Mode::HDR)) == "HDR");
    CHECK(std::string(ModeBadge(Mode::SDR)) == "SDR");
    CHECK(std::string(ModeBadge(Mode::AcmSdr)).find("ACM") != std::string::npos);
    CHECK(PipelineIsHdr(Mode::HDR));
    CHECK_FALSE(PipelineIsHdr(Mode::AcmSdr));
}

TEST_CASE("ui: number fields know the decimals they show (rounding-only commits are ignored)") {
    CHECK(ui::FormatDecimals("%.0f") == 0);
    CHECK(ui::FormatDecimals("%.4f") == 4);
    CHECK(ui::FormatDecimals("%f") == 6);
    CHECK(ui::FormatDecimals("x = %.2f nits") == 2);
    CHECK(ui::FormatDecimals("plain") == 6);
}
