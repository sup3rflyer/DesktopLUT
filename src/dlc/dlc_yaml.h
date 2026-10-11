#pragma once
// A tolerant reader for the YAML DLC writes by hand (calibration_profile.yaml): block mappings and
// sequences, comments (whole-line and end-of-line), plain / single- / double-quoted scalars, and simple
// flow collections ({ a: b }, [a, b]). Anchors, aliases, tags and multi-document streams are not
// supported; block scalars (| >) are read as their raw text. It never throws: a line it cannot make
// sense of is skipped, so a profile with an unexpected construct still yields the keys around it.
//
// Scalars stay strings (no type resolution): "2026-06-19" is not a date, "9" is not an int. Callers
// convert what they need.

#include <string>
#include <utility>
#include <vector>

namespace dlc {

struct YamlNode {
    enum Kind { Null, Scalar, Map, Seq } kind = Null;
    std::string scalar;
    std::vector<std::pair<std::string, YamlNode>> map;
    std::vector<YamlNode> seq;

    const YamlNode* find(const std::string& key) const;   // Map only; first match
    // A scalar child as text ("" when absent, null or not a scalar).
    std::string str(const std::string& key) const;
    // A scalar child as an integer; `def` when absent or not an integer.
    int integer(const std::string& key, int def) const;
    bool isNullish() const;   // Null, or a scalar "null" / "~" / ""
};

// Parses UTF-8 text (a BOM is skipped). Text larger than kMaxYamlBytes reads as empty (Null).
constexpr size_t kMaxYamlBytes = 1u << 20;
YamlNode ParseYaml(const std::string& text);

}  // namespace dlc
