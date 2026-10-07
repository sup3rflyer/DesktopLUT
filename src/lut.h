// DesktopLUT - lut.h
// LUT loading and parsing

#pragma once

#include <string>
#include <vector>
#include <d3d11.h>

// Load LUT from file (.cube or .txt format)
bool LoadLUT(const std::wstring& path, std::vector<float>& data, int& lutSize);

// Hook mode stages a canonical copy of the host's parse instead of the user's file: the DLL's parser
// (header first, unindented lines, no BOM) silently skipped files LoadLUT accepts — an eeColor .txt or
// an indented .cube showed "Active" and applied nothing (T1.5). The text is "LUT_3D_SIZE N" + N^3 lines
// of shortest round-trip floats (exact, locale-independent). Empty on invalid input.
std::string CanonicalCubeText(const std::vector<float>& data, int lutSize);
bool WriteCanonicalCube(const std::wstring& path, const std::vector<float>& data, int lutSize);

// Create 3D texture from LUT data
bool CreateLUTTexture(const std::vector<float>& data, int lutSize,
                      ID3D11Texture3D** outTexture, ID3D11ShaderResourceView** outSRV);
