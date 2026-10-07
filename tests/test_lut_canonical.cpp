// T1.5: hook mode stages a canonical .cube written from the host's own parse (LoadLUT) instead of the
// user's file. Round trip through the DLL's REAL parser (dwm_hook/hook_lut.cpp ParseLUT, compiled into
// this test exe): every file the host accepts must reach the DLL value-for-value — the DLL's parser
// (header first, unindented lines, no BOM) used to skip such files silently while the host said "Active".

#include "doctest.h"
#include "lut.h"
#include "../dwm_hook/hook_lut.h"

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>
#include <vector>

// hook_lut.cpp links against two DLL-side symbols ParseLUT never uses.
bool isWindows11_25h2 = false;
void GetMonitorPositionFromContext(void*, int& left, int& top) { left = 0; top = 0; }

namespace {

void WriteText(const char* path, const std::string& text) {
    std::ofstream f(path, std::ios::binary | std::ios::trunc);
    f.write(text.data(), (std::streamsize)text.size());
}

std::wstring Wide(const char* s) { return std::wstring(s, s + strlen(s)); }

// Host parse -> canonical text -> file -> DLL parse; true when the DLL holds exactly the host's data.
bool RoundTripsExactly(const char* srcPath, int* sizeOut = nullptr) {
    std::vector<float> host;
    int size = 0;
    if (!LoadLUT(Wide(srcPath), host, size)) return false;
    const std::string canon = CanonicalCubeText(host, size);
    if (canon.empty()) return false;
    const char* stagedPath = "lut_canonical_staged.cube";
    WriteText(stagedPath, canon);
    lutData dll{};
    char name[64];
    snprintf(name, sizeof(name), "%s", stagedPath);
    const bool parsed = ParseLUT(&dll, name);
    std::remove(stagedPath);
    if (!parsed) return false;
    bool same = (dll.size == size);
    const size_t n = (size_t)size * size * size;
    for (size_t i = 0; same && i < n; i++)
        for (int c = 0; c < 3; c++)
            if (dll.rawLut[i * 4 + c] != host[i * 4 + c]) { same = false; break; }   // exact, not approximate
    free(dll.rawLut);
    if (sizeOut) *sizeOut = size;
    return same;
}

// A 2^3 cube with awkward-but-valid values (exponents, negatives, HDR scale), in the given layout.
std::string Cube2(const std::string& header, const std::string& indent) {
    const char* rows[8] = { "0 0 0", "1.25e-07 0 0", "0 -0.25 0", "0.333333343 1 0",
                            "0 0 12.5", "1 0 1", "0 1 1", "1 1 1" };
    std::string s = header;
    for (const char* r : rows) s += indent + r + "\n";
    return s;
}

}  // namespace

TEST_CASE("Canonical cube: a file only the host could read reaches the DLL value-for-value") {
    // BOM + indented header + indented data + explicit default DOMAIN + TITLE + comments: LoadLUT reads it,
    // the DLL's parser does not (its header match needs column 0).
    const char* path = "lut_canonical_quirky.cube";
    WriteText(path, Cube2("\xEF\xBB\xBF# made by a tool\n  TITLE \"quirky\"\n  DOMAIN_MIN 0 0 0\n"
                          "  DOMAIN_MAX 1.0 1.0 1.0\n  LUT_3D_SIZE 2\n", "    "));
    lutData direct{};
    char name[64];
    snprintf(name, sizeof(name), "%s", path);
    const bool dllReadsOriginal = ParseLUT(&direct, name);
    if (dllReadsOriginal) free(direct.rawLut);
    CHECK_FALSE(dllReadsOriginal);   // the bug: staged verbatim, this applied nothing

    int size = 0;
    CHECK(RoundTripsExactly(path, &size));
    CHECK(size == 2);
    std::remove(path);
}

TEST_CASE("Canonical cube: eeColor .txt reaches the DLL (its 0..65535 scale normalised by the host)") {
    const char* path = "lut_canonical_eecolor.txt";
    std::string text;
    text.reserve(65 * 65 * 65 * 18);
    for (int b = 0; b < 65; b++)
        for (int g = 0; g < 65; g++)
            for (int r = 0; r < 65; r++)
                text += std::to_string(r * 1023) + " " + std::to_string(g * 1023) + " " + std::to_string(b * 1023) + "\n";
    WriteText(path, text);
    int size = 0;
    CHECK(RoundTripsExactly(path, &size));
    CHECK(size == 65);
    std::remove(path);
}

TEST_CASE("Canonical cube: canonical text is locale-free, header first, one unindented entry per line") {
    std::vector<float> data;
    for (int i = 0; i < 8; i++) { data.push_back(0.5f); data.push_back(-1e-7f); data.push_back(1234.5f); data.push_back(1.0f); }
    const std::string text = CanonicalCubeText(data, 2);
    REQUIRE_FALSE(text.empty());
    const size_t header = text.find("LUT_3D_SIZE 2\n");
    REQUIRE(header != std::string::npos);
    const std::string body = text.substr(header);
    CHECK(body.find(',') == std::string::npos);   // never a decimal comma
    CHECK(text.find("\n ") == std::string::npos);
    CHECK(text.find("\n0.5 -1e-07 1234.5\n") != std::string::npos);
    // Invalid input yields nothing to stage
    CHECK(CanonicalCubeText(data, 3).empty());
    std::vector<float> bad = data;
    bad[1] = std::numeric_limits<float>::infinity();
    CHECK(CanonicalCubeText(bad, 2).empty());
}

TEST_CASE("LoadLUT: a non-default DOMAIN is rejected (both renderers assume the 0..1 input domain)") {
    const char* path = "lut_canonical_domain.cube";
    WriteText(path, Cube2("LUT_3D_SIZE 2\nDOMAIN_MAX 2 2 2\n", ""));
    std::vector<float> data;
    int size = 0;
    CHECK_FALSE(LoadLUT(Wide(path), data, size));
    WriteText(path, Cube2("LUT_3D_SIZE 2\nDOMAIN_MIN -0.5 0 0\n", ""));
    CHECK_FALSE(LoadLUT(Wide(path), data, size));
    WriteText(path, Cube2("LUT_3D_SIZE 2\nDOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1 1 1\n", ""));
    CHECK(LoadLUT(Wide(path), data, size));
    std::remove(path);
}
