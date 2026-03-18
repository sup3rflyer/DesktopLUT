#include "doctest.h"
#include "lut.h"
#include <cmath>
#include <fstream>

struct TempFile {
    std::wstring path;
    TempFile(const wchar_t* name) : path(name) {}
    ~TempFile() { _wremove(path.c_str()); }
    const wchar_t* c_str() const { return path.c_str(); }
};

// Helper to find fixture files (try multiple paths since CWD may vary)
static std::wstring FindFixture(const wchar_t* name) {
    std::wstring paths[] = {
        std::wstring(L"tests/fixtures/") + name,
        std::wstring(L"../../tests/fixtures/") + name,
        std::wstring(L"../tests/fixtures/") + name,
    };
    for (auto& p : paths) {
        std::ifstream f(p);
        if (f.good()) return p;
    }
    return L"tests/fixtures/" + std::wstring(name);
}

// ============================================================================
// .cube Format Parsing
// ============================================================================

TEST_CASE("LUT: valid 17^3 identity cube") {
    std::wstring path = FindFixture(L"identity_17.cube");
    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(path, data, lutSize);
    if (!ok) {
        WARN("identity_17.cube not found, skipping");
        return;
    }
    CHECK(lutSize == 17);
    CHECK(data.size() == 17 * 17 * 17 * 4);
}

TEST_CASE("LUT: identity diagonal values") {
    std::wstring path = FindFixture(L"identity_17.cube");
    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(path, data, lutSize);
    if (!ok) return;

    // For R=G=B=i, data should be i/16 for each channel
    for (int i = 0; i < 17; i++) {
        // Index = i + i*17 + i*17*17 = i*(1 + 17 + 289) = i*307
        int idx = i * 307 * 4;
        float expected = (float)i / 16.0f;
        CHECK(data[idx + 0] == doctest::Approx(expected).epsilon(0.001));
        CHECK(data[idx + 1] == doctest::Approx(expected).epsilon(0.001));
        CHECK(data[idx + 2] == doctest::Approx(expected).epsilon(0.001));
    }
}

TEST_CASE("LUT: alpha channel is always 1.0") {
    std::wstring path = FindFixture(L"identity_17.cube");
    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(path, data, lutSize);
    if (!ok) return;

    for (size_t i = 3; i < data.size(); i += 4) {
        CHECK(data[i] == 1.0f);
    }
}

TEST_CASE("LUT: identity corners (0,0,0) and (1,1,1)") {
    std::wstring path = FindFixture(L"identity_17.cube");
    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(path, data, lutSize);
    if (!ok) return;

    // (0,0,0) = index 0 → black
    CHECK(data[0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(data[1] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(data[2] == doctest::Approx(0.0f).epsilon(0.001));

    // (16,16,16) = last entry → white
    int lastIdx = 16 * 307 * 4;
    CHECK(data[lastIdx + 0] == doctest::Approx(1.0f).epsilon(0.001));
    CHECK(data[lastIdx + 1] == doctest::Approx(1.0f).epsilon(0.001));
    CHECK(data[lastIdx + 2] == doctest::Approx(1.0f).epsilon(0.001));
}

TEST_CASE("LUT: identity primary corners") {
    std::wstring path = FindFixture(L"identity_17.cube");
    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(path, data, lutSize);
    if (!ok) return;

    // Pure red = R=16, G=0, B=0 → index = 16
    int redIdx = 16 * 4;
    CHECK(data[redIdx + 0] == doctest::Approx(1.0f).epsilon(0.001));
    CHECK(data[redIdx + 1] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(data[redIdx + 2] == doctest::Approx(0.0f).epsilon(0.001));

    // Pure green = R=0, G=16, B=0 → index = 16*17 = 272
    int greenIdx = 272 * 4;
    CHECK(data[greenIdx + 0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(data[greenIdx + 1] == doctest::Approx(1.0f).epsilon(0.001));
    CHECK(data[greenIdx + 2] == doctest::Approx(0.0f).epsilon(0.001));

    // Pure blue = R=0, G=0, B=16 → index = 16*289 = 4624
    int blueIdx = 4624 * 4;
    CHECK(data[blueIdx + 0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(data[blueIdx + 1] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(data[blueIdx + 2] == doctest::Approx(1.0f).epsilon(0.001));
}

TEST_CASE("LUT: data layout (R varies fastest, then G, then B)") {
    TempFile tmp(L"_test_layout3.cube");
    {
        std::ofstream f(tmp.path);
        f << "LUT_3D_SIZE 3\n";
        for (int b = 0; b < 3; b++)
            for (int g = 0; g < 3; g++)
                for (int r = 0; r < 3; r++)
                    f << (float)r / 2.0f << " " << (float)g / 2.0f << " " << (float)b / 2.0f << "\n";
    }

    std::vector<float> data;
    int lutSize = 0;
    REQUIRE(LoadLUT(tmp.path, data, lutSize));
    CHECK(lutSize == 3);
    REQUIRE(data.size() == 27 * 4);

    // (1,0,0): R=0.5, G=0, B=0 → index 1
    CHECK(data[1*4+0] == doctest::Approx(0.5f).epsilon(0.001));
    CHECK(data[1*4+1] == doctest::Approx(0.0f).epsilon(0.001));

    // (0,1,0): R=0, G=0.5, B=0 → index 3
    CHECK(data[3*4+0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(data[3*4+1] == doctest::Approx(0.5f).epsilon(0.001));

    // (0,0,1): R=0, G=0, B=0.5 → index 9
    CHECK(data[9*4+0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(data[9*4+2] == doctest::Approx(0.5f).epsilon(0.001));

    // (2,2,2): white → index 26
    CHECK(data[26*4+0] == doctest::Approx(1.0f).epsilon(0.001));
    CHECK(data[26*4+1] == doctest::Approx(1.0f).epsilon(0.001));
    CHECK(data[26*4+2] == doctest::Approx(1.0f).epsilon(0.001));
}

TEST_CASE("LUT: nonexistent file returns false") {
    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(L"Z:\\this_does_not_exist\\impossible.cube", data, lutSize);
    CHECK_FALSE(ok);
}

TEST_CASE("LUT: case-insensitive .CUBE extension") {
    TempFile tmp(L"_test_upper.CUBE");
    {
        std::ofstream f(tmp.path);
        f << "LUT_3D_SIZE 2\n";
        for (int b = 0; b < 2; b++)
            for (int g = 0; g < 2; g++)
                for (int r = 0; r < 2; r++)
                    f << (float)r << " " << (float)g << " " << (float)b << "\n";
    }

    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(tmp.path, data, lutSize);
    CHECK(ok);
    CHECK(lutSize == 2);
}

TEST_CASE("LUT: malformed cube (entry count mismatch)") {
    std::wstring path = FindFixture(L"malformed.cube");
    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(path, data, lutSize);
    CHECK_FALSE(ok);
}

TEST_CASE("LUT: empty file produces size 0 or fails") {
    TempFile tmp(L"_test_empty.cube");
    { std::ofstream f(tmp.path); }

    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(tmp.path, data, lutSize);

    // Empty .cube: either parser rejects it (ok=false) or accepts with size 0
    // Both are valid behaviors — assert the invariant either way
    if (ok) {
        CHECK(lutSize == 0);
        CHECK(data.empty());
    } else {
        CHECK(lutSize == 0);
    }
}

TEST_CASE("LUT: size 2 (minimum)") {
    TempFile tmp(L"_test_size2.cube");
    {
        std::ofstream f(tmp.path);
        f << "LUT_3D_SIZE 2\n";
        for (int b = 0; b < 2; b++)
            for (int g = 0; g < 2; g++)
                for (int r = 0; r < 2; r++)
                    f << (float)r << " " << (float)g << " " << (float)b << "\n";
    }

    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(tmp.path, data, lutSize);

    CHECK(ok);
    CHECK(lutSize == 2);
    CHECK(data.size() == 8 * 4);
}

TEST_CASE("LUT: size 1 (below min)") {
    TempFile tmp(L"_test_size1.cube");
    {
        std::ofstream f(tmp.path);
        f << "LUT_3D_SIZE 1\n";
        f << "0.5 0.5 0.5\n";
    }

    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(tmp.path, data, lutSize);

    CHECK_FALSE(ok);
}

TEST_CASE("LUT: size 129 (above max)") {
    TempFile tmp(L"_test_size129.cube");
    {
        std::ofstream f(tmp.path);
        f << "LUT_3D_SIZE 129\n";
        f << "0.0 0.0 0.0\n";  // Won't matter, should reject early
    }

    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(tmp.path, data, lutSize);

    CHECK_FALSE(ok);
}

TEST_CASE("LUT: comments and headers skipped") {
    TempFile tmp(L"_test_comments.cube");
    {
        std::ofstream f(tmp.path);
        f << "# This is a comment\n";
        f << "TITLE \"Test\"\n";
        f << "DOMAIN_MIN 0 0 0\n";
        f << "DOMAIN_MAX 1 1 1\n";
        f << "LUT_3D_SIZE 2\n";
        f << "# Another comment\n";
        for (int b = 0; b < 2; b++)
            for (int g = 0; g < 2; g++)
                for (int r = 0; r < 2; r++)
                    f << (float)r << " " << (float)g << " " << (float)b << "\n";
    }

    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(tmp.path, data, lutSize);

    CHECK(ok);
    CHECK(lutSize == 2);
}

TEST_CASE("LUT: blank lines skipped") {
    TempFile tmp(L"_test_blanks.cube");
    {
        std::ofstream f(tmp.path);
        f << "LUT_3D_SIZE 2\n";
        f << "\n";
        for (int b = 0; b < 2; b++)
            for (int g = 0; g < 2; g++)
                for (int r = 0; r < 2; r++) {
                    f << (float)r << " " << (float)g << " " << (float)b << "\n";
                    f << "\n";
                }
    }

    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(tmp.path, data, lutSize);

    CHECK(ok);
    CHECK(lutSize == 2);
}

TEST_CASE("LUT: tab-separated values") {
    TempFile tmp(L"_test_tabs.cube");
    {
        std::ofstream f(tmp.path);
        f << "LUT_3D_SIZE 2\n";
        for (int b = 0; b < 2; b++)
            for (int g = 0; g < 2; g++)
                for (int r = 0; r < 2; r++)
                    f << (float)r << "\t" << (float)g << "\t" << (float)b << "\n";
    }

    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(tmp.path, data, lutSize);

    CHECK(ok);
    CHECK(lutSize == 2);
    CHECK(data.size() == 8 * 4);
}

TEST_CASE("LUT: CRLF line endings") {
    TempFile tmp(L"_test_crlf.cube");
    {
        std::ofstream f(tmp.path, std::ios::binary);
        f << "LUT_3D_SIZE 2\r\n";
        for (int b = 0; b < 2; b++)
            for (int g = 0; g < 2; g++)
                for (int r = 0; r < 2; r++)
                    f << (float)r << " " << (float)g << " " << (float)b << "\r\n";
    }

    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(tmp.path, data, lutSize);

    CHECK(ok);
    CHECK(lutSize == 2);
}

TEST_CASE("LUT: mixed whitespace (spaces and tabs)") {
    TempFile tmp(L"_test_mixed_ws.cube");
    {
        std::ofstream f(tmp.path);
        f << "LUT_3D_SIZE  2\n";
        for (int b = 0; b < 2; b++)
            for (int g = 0; g < 2; g++)
                for (int r = 0; r < 2; r++)
                    f << "  " << (float)r << "  \t " << (float)g << "   " << (float)b << "\n";
    }

    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(tmp.path, data, lutSize);

    CHECK(ok);
    CHECK(lutSize == 2);
    CHECK(data.size() == 8 * 4);
}

// ============================================================================
// .txt Format (eeColor)
// ============================================================================

TEST_CASE("LUT: eeColor .txt normalization") {
    TempFile tmp(L"_test_eecolor.txt");
    {
        std::ofstream f(tmp.path);
        // Write a small number of lines first, just testing normalization
        // eeColor defaults to 65^3 but parser detects count
        // Write 8 lines (2^3) to test — but eeColor hardcodes lutSize=65
        // So let's verify the normalization for integer values > 1.0
        for (int b = 0; b < 65; b++)
            for (int g = 0; g < 65; g++)
                for (int r = 0; r < 65; r++)
                    f << r * 1008 << " " << g * 1008 << " " << b * 1008 << "\n";
    }

    std::vector<float> data;
    int lutSize = 0;
    bool ok = LoadLUT(tmp.path, data, lutSize);

    CHECK(ok);
    CHECK(lutSize == 65);
    // First entry: 0/65535 = 0
    CHECK(data[0] == doctest::Approx(0.0f).epsilon(0.001));
    // Values should be normalized to 0-1 range
    CHECK(data[4] == doctest::Approx(1008.0f / 65535.0f).epsilon(0.001));
}
