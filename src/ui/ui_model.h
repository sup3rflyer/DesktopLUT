#pragma once
// Pure view-model logic for the ImGui interface: no Windows, D3D or ImGui calls, so it is unit-tested
// (tests/test_ui_model.cpp). The views turn its output into pixels.

#include "types.h"
#include <string>
#include <vector>

namespace ui {

// Live colour mode of a display (displayconfig.h DisplayColorMode, decoupled for tests).
enum class Mode { Unknown, SDR, AcmSdr, HDR };

const char* ModeBadge(Mode m);   // "HDR" / "SDR" / "SDR · ACM" / "—"

// State of one layer as the interface reports it. Never claim more than is observed:
//   None        nothing is set: no light at all
//   Off         set, but switched off (its stage switch) or suspended: a ring
//   Configured  changes the picture now (as far as DesktopLUT can tell): a filled dot
//   Fault       should be changing the picture and is not reaching the screen: an amber triangle
//   NotApplicable  a marker, not a layer (Desktop / Scanout / Panel)
// Verified / Pending are kept for states a measurement can confirm.
enum class Light { None, Off, Configured, Verified, Pending, Fault, NotApplicable };

enum class StageId { Desktop, ToneMap, Lut3D, Fald, ShaderTweak, Scanout, Primaries, Grayscale, Panel };

struct Stage {
    StageId id;
    Light light = Light::Off;
    bool selectable = true;   // false for the Desktop / Scanout / Panel markers
    bool dimmed = false;      // configured but disabled (FALD file set, layer off) or not applicable
};

// Which side of the per-mode settings a pipeline shows: HDR uses the hdr* fields; SDR and ACM SDR
// both use the sdr* fields.
inline bool PipelineIsHdr(Mode shown) { return shown == Mode::HDR; }

// What the live layers (tone map, 3D LUT, FALD) are doing now. The MHC stages do not depend on it.
struct PathRuntime {
    bool suspended = false;    // the user's Suspend: the live layers do not run
    bool running = false;      // the live layers run
    bool hookMode = false;     // the DWM hook engine (else the overlay)
    bool hookActive = false;   // the hook is injected
};

// The pixel path for one display, in pixel order: what the desktop goes through on its way to the
// panel. `shown` is the mode whose settings are displayed (the live mode, or the other one when the
// user edits it); `live` is the display's actual mode (FALD only runs on FP16 back buffers). Faults
// from `rt` are reported only for the mode the display is in.
std::vector<Stage> BuildPixelPath(const MonitorSettings& ms, Mode shown, Mode live, const PathRuntime& rt = {});

// Whether an MHC stage has anything to do (before its switch): primaries = the primaries correction or a
// non-D65 white balance; grayscale = an imported curve, a base or correction grayscale, or desktop gamma.
bool MhcPrimariesContent(const MHCSettings& mhc);
bool MhcGrayscaleContent(const MHCSettings& mhc, bool hdr);

const char* StageLabel(StageId id);   // caps label for the strip

// Configured state of the MHC profile for one mode (both MHC stages share it: one ICC profile).
Light MhcLight(const MHCSettings& mhc);

// One light for several layers: the most telling of them (Fault, then Configured, then Off, then None).
Light CombineLights(Light a, Light b);

// True when the overlay shader-tweak layer (pipe runtime.set_grayscale_tweak / INI) is active for a mode.
bool ShaderTweakActive(const ColorCorrectionSettings& cc);

// Name to show for a display. The OS friendly name (EDID name descriptor via DisplayConfig) when there
// is one; many EDIDs (and CRU overrides) carry none, so otherwise the EDID maker + product code from the
// hardware id ("ASUS 322A"); "Display" when even that is unknown.
std::wstring DisplayLabel(const DisplayIdentity& id, const std::wstring& osName);

}  // namespace ui
