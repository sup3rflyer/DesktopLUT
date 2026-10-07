// DesktopLUT - ipc_json.h
// Minimal, self-contained JSON for the calibration pipe (no external dependency).
// Header-only so the test project can exercise the parser directly.
//
// The parser reads untrusted input from a named-pipe client: it is bounded in nesting
// depth (the recursion would otherwise overflow the pipe thread's stack on "[[[[..." —
// a stack overflow is not a C++ exception, so it would take the whole 24/7 process down)
// and in value count, and it rejects trailing characters, malformed numbers and numbers
// outside the double range.

#pragma once

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace ipc_json {

constexpr int    kMaxDepth  = 64;        // arrays/objects nested inside each other
constexpr size_t kMaxValues = 200000;    // every value parsed (requests are capped at 256 KiB)

struct JsonValue {
    enum Type { Null, Bool, Num, Str, Arr, Obj } type = Null;
    bool b = false;
    double num = 0.0;
    std::string str;
    std::vector<JsonValue> arr;
    std::vector<std::pair<std::string, JsonValue>> members;

    const JsonValue* find(const std::string& key) const {
        if (type != Obj) return nullptr;
        for (const auto& kv : members)
            if (kv.first == key) return &kv.second;
        return nullptr;
    }
    bool has(const std::string& key) const { return find(key) != nullptr; }
    std::string getStr(const std::string& key, const std::string& def = "") const {
        const JsonValue* v = find(key);
        return (v && v->type == Str) ? v->str : def;
    }
    double getNum(const std::string& key, double def = 0.0) const {
        const JsonValue* v = find(key);
        return (v && v->type == Num) ? v->num : def;
    }
    int getInt(const std::string& key, int def = 0) const {
        const JsonValue* v = find(key);
        return (v && v->type == Num) ? (int)std::llround(v->num) : def;
    }
    void set(const std::string& key, JsonValue v) { members.emplace_back(key, std::move(v)); }
};

inline JsonValue JBool(bool v) { JsonValue j; j.type = JsonValue::Bool; j.b = v; return j; }
inline JsonValue JNum(double v) { JsonValue j; j.type = JsonValue::Num; j.num = v; return j; }
inline JsonValue JStr(const std::string& v) { JsonValue j; j.type = JsonValue::Str; j.str = v; return j; }
inline JsonValue JObj() { JsonValue j; j.type = JsonValue::Obj; return j; }
inline JsonValue JArr() { JsonValue j; j.type = JsonValue::Arr; return j; }

inline void AppendUtf8(std::string& out, unsigned cp) {
    if (cp <= 0x7F) {
        out += (char)cp;
    } else if (cp <= 0x7FF) {
        out += (char)(0xC0 | (cp >> 6));
        out += (char)(0x80 | (cp & 0x3F));
    } else if (cp <= 0xFFFF) {
        out += (char)(0xE0 | (cp >> 12));
        out += (char)(0x80 | ((cp >> 6) & 0x3F));
        out += (char)(0x80 | (cp & 0x3F));
    } else {
        out += (char)(0xF0 | (cp >> 18));
        out += (char)(0x80 | ((cp >> 12) & 0x3F));
        out += (char)(0x80 | ((cp >> 6) & 0x3F));
        out += (char)(0x80 | (cp & 0x3F));
    }
}

struct JsonParser {
    const std::string& s;
    size_t i = 0;
    int depth = 0;
    size_t values = 0;
    explicit JsonParser(const std::string& str) : s(str) {}

    [[noreturn]] void err(const char* m) { throw std::runtime_error(m); }
    void ws() {
        while (i < s.size() && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) i++;
    }
    // One complete value; anything but whitespace after it is an error.
    JsonValue parse() {
        ws();
        JsonValue v = value();
        ws();
        if (i != s.size()) err("unexpected characters after the JSON value");
        return v;
    }

    JsonValue value() {
        ws();
        if (i >= s.size()) err("unexpected end of input");
        if (++values > kMaxValues) err("too many values");
        char c = s[i];
        if (c == '{') return object();
        if (c == '[') return array();
        if (c == '"') return JStr(string());
        if (c == 't') { literal("true"); return JBool(true); }
        if (c == 'f') { literal("false"); return JBool(false); }
        if (c == 'n') { literal("null"); return JsonValue(); }
        return number();
    }
    void literal(const char* lit) {
        for (const char* p = lit; *p; ++p) {
            if (i >= s.size() || s[i] != *p) err("invalid literal");
            i++;
        }
    }
    unsigned hex4() {
        if (i + 4 > s.size()) err("bad \\u escape");
        unsigned v = 0;
        for (int k = 0; k < 4; ++k) {
            char c = s[i++];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= (c - '0');
            else if (c >= 'a' && c <= 'f') v |= (c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= (c - 'A' + 10);
            else err("bad hex digit");
        }
        return v;
    }
    std::string string() {
        if (i >= s.size() || s[i] != '"') err("expected string");
        i++;
        std::string out;
        while (i < s.size()) {
            char c = s[i++];
            if (c == '"') return out;
            if (c == '\\') {
                if (i >= s.size()) err("bad escape");
                char e = s[i++];
                switch (e) {
                    case '"': out += '"'; break;
                    case '\\': out += '\\'; break;
                    case '/': out += '/'; break;
                    case 'n': out += '\n'; break;
                    case 't': out += '\t'; break;
                    case 'r': out += '\r'; break;
                    case 'b': out += '\b'; break;
                    case 'f': out += '\f'; break;
                    case 'u': {
                        unsigned cp = hex4();
                        if (cp >= 0xD800 && cp <= 0xDBFF && i + 1 < s.size() && s[i] == '\\' && s[i + 1] == 'u') {
                            i += 2;
                            unsigned lo = hex4();
                            if (lo >= 0xDC00 && lo <= 0xDFFF)
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        }
                        AppendUtf8(out, cp);
                        break;
                    }
                    default: err("bad escape");
                }
            } else {
                out += c;
            }
        }
        err("unterminated string");
    }
    JsonValue number() {
        size_t start = i;
        if (i < s.size() && s[i] == '-') i++;
        while (i < s.size() &&
               ((s[i] >= '0' && s[i] <= '9') || s[i] == '.' || s[i] == 'e' || s[i] == 'E' || s[i] == '+' || s[i] == '-'))
            i++;
        if (i == start) err("invalid number");
        // The scan above is permissive; strtod must consume the whole token ("1.2.3", "-",
        // "1e" are rejected) and the value must be finite ("1e400" would be inf).
        const std::string tok = s.substr(start, i - start);
        char* end = nullptr;
        double d = std::strtod(tok.c_str(), &end);
        if (end != tok.c_str() + tok.size()) err("invalid number");
        if (!std::isfinite(d)) err("number out of range");
        return JNum(d);
    }
    JsonValue array() {
        if (++depth > kMaxDepth) err("nesting too deep");
        JsonValue v = JArr();
        i++;  // [
        ws();
        if (i < s.size() && s[i] == ']') { i++; depth--; return v; }
        while (true) {
            v.arr.push_back(value());
            ws();
            if (i >= s.size()) err("unterminated array");
            if (s[i] == ',') { i++; continue; }
            if (s[i] == ']') { i++; break; }
            err("expected , or ]");
        }
        depth--;
        return v;
    }
    JsonValue object() {
        if (++depth > kMaxDepth) err("nesting too deep");
        JsonValue v = JObj();
        i++;  // {
        ws();
        if (i < s.size() && s[i] == '}') { i++; depth--; return v; }
        while (true) {
            ws();
            std::string key = string();
            ws();
            if (i >= s.size() || s[i] != ':') err("expected :");
            i++;
            v.members.emplace_back(key, value());
            ws();
            if (i >= s.size()) err("unterminated object");
            if (s[i] == ',') { i++; continue; }
            if (s[i] == '}') { i++; break; }
            err("expected , or }");
        }
        depth--;
        return v;
    }
};

inline void SerializeStr(const std::string& s, std::string& out) {
    out += '"';
    for (unsigned char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += (char)c;
                }
        }
    }
    out += '"';
}

inline void Serialize(const JsonValue& v, std::string& out) {
    switch (v.type) {
        case JsonValue::Null: out += "null"; break;
        case JsonValue::Bool: out += v.b ? "true" : "false"; break;
        case JsonValue::Num: {
            // JSON has no inf/nan; never emit a token the client cannot parse.
            if (!std::isfinite(v.num)) { out += "null"; break; }
            char buf[40];
            std::snprintf(buf, sizeof(buf), "%.10g", v.num);
            out += buf;
            break;
        }
        case JsonValue::Str: SerializeStr(v.str, out); break;
        case JsonValue::Arr: {
            out += '[';
            for (size_t k = 0; k < v.arr.size(); ++k) { if (k) out += ','; Serialize(v.arr[k], out); }
            out += ']';
            break;
        }
        case JsonValue::Obj: {
            out += '{';
            for (size_t k = 0; k < v.members.size(); ++k) {
                if (k) out += ',';
                SerializeStr(v.members[k].first, out);
                out += ':';
                Serialize(v.members[k].second, out);
            }
            out += '}';
            break;
        }
    }
}

}  // namespace ipc_json
