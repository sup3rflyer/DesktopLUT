// DesktopLUT - lut.h
// LUT loading and parsing

#pragma once

#include <string>
#include <vector>
#include <d3d11.h>

// Load LUT from file (.cube or .txt format)
bool LoadLUT(const std::wstring& path, std::vector<float>& data, int& lutSize);

// Create 3D texture from LUT data
bool CreateLUTTexture(const std::vector<float>& data, int lutSize,
                      ID3D11Texture3D** outTexture, ID3D11ShaderResourceView** outSRV);
