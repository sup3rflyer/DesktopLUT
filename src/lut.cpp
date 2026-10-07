// DesktopLUT - lut.cpp
// LUT loading and parsing

#include "lut.h"
#include "globals.h"
#include <fstream>
#include <sstream>
#include <iostream>
#include <locale>
#include <DirectXPackedVector.h>

bool LoadLUT(const std::wstring& path, std::vector<float>& data, int& lutSize) {
    std::ifstream file(path);
    if (!file.is_open()) {
        std::wcerr << L"Failed to open LUT file: " << path << std::endl;
        return false;
    }

    data.clear();
    lutSize = 0;

    // Detect format by extension (case-insensitive)
    bool isCube = false;
    if (path.size() > 5) {
        std::wstring ext = path.substr(path.size() - 5);
        for (auto& c : ext) c = towlower(c);
        isCube = (ext == L".cube");
    }

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
                iss.imbue(std::locale::classic());
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
            iss.imbue(std::locale::classic());
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
            iss.imbue(std::locale::classic());
            float r, g, b;
            if (iss >> r >> g >> b) {
                data.push_back(r);
                data.push_back(g);
                data.push_back(b);
                data.push_back(1.0f);
                count++;
            }
        }

        // Single-pass normalization: if any RGB value exceeds 1.5 it's clearly integer
        // range (e.g. eeColor 0..65535). Normalize RGB by the max but SKIP the alpha
        // column (every 4th element) so alpha stays exactly 1.0 — dividing it too would
        // leave alpha ~1.5e-5 and break the "alpha is always 1.0" invariant.
        float maxVal = 0.0f;
        for (size_t i = 0; i < data.size(); i++) {
            if (i % 4 != 3) maxVal = (std::max)(maxVal, data[i]);
        }
        if (maxVal > 1.5f) {
            for (size_t i = 0; i < data.size(); i++) {
                if (i % 4 != 3) data[i] /= maxVal;
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
    // FP32, like the DWM hook's LUT texture: an FP16 texel carries only an 11-bit significand, up to
    // a quarter of a 10-bit step of error in the HDR path, and the overlay is what previews and
    // verifies a cube the hook then applies at full precision. 65^3 x 16 B = 4.4 MB. Falls back to
    // FP16 only on a device that cannot sample a filtered FP32 3D texture.
    UINT fp32Support = 0;
    const bool useFp32 = SUCCEEDED(g_device->CheckFormatSupport(DXGI_FORMAT_R32G32B32A32_FLOAT, &fp32Support)) &&
                         (fp32Support & D3D11_FORMAT_SUPPORT_TEXTURE3D) &&
                         (fp32Support & D3D11_FORMAT_SUPPORT_SHADER_SAMPLE);
    std::vector<uint16_t> halfData;
    if (!useFp32) {
        halfData.reserve(data.size());
        for (float f : data) halfData.push_back(DirectX::PackedVector::XMConvertFloatToHalf(f));
        std::cout << "3D LUT texture: FP32 sampling unsupported on this device, using FP16" << std::endl;
    }

    D3D11_TEXTURE3D_DESC texDesc = {};
    texDesc.Width = lutSize;
    texDesc.Height = lutSize;
    texDesc.Depth = lutSize;
    texDesc.MipLevels = 1;
    texDesc.Format = useFp32 ? DXGI_FORMAT_R32G32B32A32_FLOAT : DXGI_FORMAT_R16G16B16A16_FLOAT;
    texDesc.Usage = D3D11_USAGE_IMMUTABLE;
    texDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

    D3D11_SUBRESOURCE_DATA initData = {};
    const size_t texelBytes = useFp32 ? 4 * sizeof(float) : 4 * sizeof(uint16_t);   // RGBA
    initData.pSysMem = useFp32 ? (const void*)data.data() : (const void*)halfData.data();
    initData.SysMemPitch = (UINT)(lutSize * texelBytes);
    initData.SysMemSlicePitch = (UINT)(lutSize * lutSize * texelBytes);

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
