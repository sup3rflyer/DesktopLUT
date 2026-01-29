// DesktopLUT - lut.cpp
// LUT loading and parsing

#include "lut.h"
#include "globals.h"
#include <fstream>
#include <sstream>
#include <iostream>
#include <DirectXPackedVector.h>

bool LoadLUT(const std::wstring& path, std::vector<float>& data, int& lutSize) {
    std::ifstream file(path);
    if (!file.is_open()) {
        std::wcerr << L"Failed to open LUT file: " << path << std::endl;
        return false;
    }

    data.clear();
    lutSize = 0;

    // Detect format by extension
    bool isCube = path.size() > 5 &&
        (path.substr(path.size() - 5) == L".cube" || path.substr(path.size() - 5) == L".CUBE");

    std::string line;
    int count = 0;

    if (isCube) {
        // Parse .cube format
        while (std::getline(file, line)) {
            // Skip empty lines
            if (line.empty()) continue;

            // Skip comments
            if (line[0] == '#') continue;

            // Parse header
            if (line.find("TITLE") == 0) continue;
            if (line.find("DOMAIN_MIN") == 0) continue;
            if (line.find("DOMAIN_MAX") == 0) continue;

            if (line.find("LUT_3D_SIZE") == 0) {
                std::istringstream iss(line.substr(11));
                iss >> lutSize;
                // Validate LUT size (reasonable range: 2-128, typical values: 17, 33, 65)
                // 128^3 = 8MB texture, 256^3 = 64MB which is excessive
                if (lutSize < 2 || lutSize > 128) {
                    std::cerr << "Invalid LUT size: " << lutSize << " (must be 2-128)" << std::endl;
                    return false;
                }
                try {
                    data.reserve((size_t)lutSize * lutSize * lutSize * 4);
                } catch (const std::bad_alloc&) {
                    std::cerr << "Failed to allocate memory for " << lutSize << "^3 LUT" << std::endl;
                    return false;
                }
                continue;
            }

            // Skip 1D LUT entries if present
            if (line.find("LUT_1D_SIZE") == 0) continue;
            if (line.find("LUT_1D_INPUT_RANGE") == 0) continue;

            // Parse RGB values
            std::istringstream iss(line);
            float r, g, b;
            if (iss >> r >> g >> b) {
                data.push_back(r);
                data.push_back(g);
                data.push_back(b);
                data.push_back(1.0f);
                count++;
            }
        }
    } else {
        // Parse eeColor .txt format (65^3)
        lutSize = 65;
        try {
            data.reserve(65 * 65 * 65 * 4);
        } catch (const std::bad_alloc&) {
            std::cerr << "Failed to allocate memory for 65^3 LUT" << std::endl;
            return false;
        }

        while (std::getline(file, line)) {
            if (line.empty() || line[0] == '#') continue;

            std::istringstream iss(line);
            float r, g, b;
            if (iss >> r >> g >> b) {
                // Normalize if values are in 0-65535 range
                if (r > 1.0f || g > 1.0f || b > 1.0f) {
                    r /= 65535.0f;
                    g /= 65535.0f;
                    b /= 65535.0f;
                }
                data.push_back(r);
                data.push_back(g);
                data.push_back(b);
                data.push_back(1.0f);
                count++;
            }
        }
    }

    int expected = lutSize * lutSize * lutSize;
    if (count != expected) {
        std::cerr << "LUT error: Expected " << expected << " entries, got " << count << std::endl;
        return false;
    }

    std::cout << "Loaded " << lutSize << "^3 LUT with " << count << " entries" << std::endl;
    return true;
}

bool CreateLUTTexture(const std::vector<float>& data, int lutSize,
                      ID3D11Texture3D** outTexture, ID3D11ShaderResourceView** outSRV) {
    // Convert FP32 data to FP16 for GPU efficiency
    // Half-float is sufficient for LUT precision (10-bit mantissa = 1024 levels)
    // Industry standard: DaVinci, ACES, Baselight all use FP16 for LUT interchange
    std::vector<uint16_t> halfData;
    halfData.reserve(data.size());
    for (float f : data) {
        halfData.push_back(DirectX::PackedVector::XMConvertFloatToHalf(f));
    }

    D3D11_TEXTURE3D_DESC texDesc = {};
    texDesc.Width = lutSize;
    texDesc.Height = lutSize;
    texDesc.Depth = lutSize;
    texDesc.MipLevels = 1;
    texDesc.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;  // FP16: 50% memory vs FP32
    texDesc.Usage = D3D11_USAGE_IMMUTABLE;
    texDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

    D3D11_SUBRESOURCE_DATA initData = {};
    initData.pSysMem = halfData.data();
    initData.SysMemPitch = lutSize * 4 * sizeof(uint16_t);       // 4 components × 2 bytes
    initData.SysMemSlicePitch = lutSize * lutSize * 4 * sizeof(uint16_t);

    HRESULT hr = g_device->CreateTexture3D(&texDesc, &initData, outTexture);
    if (FAILED(hr)) {
        std::cerr << "Failed to create 3D LUT texture" << std::endl;
        return false;
    }

    hr = g_device->CreateShaderResourceView(*outTexture, nullptr, outSRV);
    if (FAILED(hr)) {
        std::cerr << "Failed to create LUT SRV" << std::endl;
        (*outTexture)->Release();
        *outTexture = nullptr;
        return false;
    }

    return true;
}
