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
// Configured = set up, but nothing confirms it reaches the screen.
enum class Light { Off, Configured, Verified, Pending, Fault, NotApplicable };

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

// The pixel path for one display, in pixel order: what the desktop goes through on its way to the
// panel. `shown` is the mode whose settings are displayed (the live mode, or the other one when the
// user edits it); `live` is the display's actual mode (FALD only runs on FP16 back buffers).
std::vector<Stage> BuildPixelPath(const MonitorSettings& ms, Mode shown, Mode live);

const char* StageLabel(StageId id);   // caps label for the strip

// Configured state of the MHC profile for one mode (both MHC stages share it: one ICC profile).
Light MhcLight(const MHCSettings& mhc);

// True when the overlay shader-tweak layer (pipe runtime.set_grayscale_tweak / INI) is active for a mode.
bool ShaderTweakActive(const ColorCorrectionSettings& cc);

}  // namespace ui
