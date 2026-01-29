// DesktopLUT - color.h
// Color space mathematics and primaries calculations

#pragma once

#include "types.h"

// Calculate 3x3 color space conversion matrix using Bradford chromatic adaptation
// Converts from source primaries (content) to target primaries (display)
void CalculatePrimariesMatrix(const DisplayPrimariesData& src, const DisplayPrimariesData& tgt, float* outMatrix);
