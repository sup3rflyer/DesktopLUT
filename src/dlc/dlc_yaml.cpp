#include "dlc/dlc_yaml.h"

#include <cstdlib>

namespace dlc {

namespace {

struct Line {
    int indent = 0;
    std::string text;   // without indentation and comment, right-trimmed
};

std::string Trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t')) a++;
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\r')) b--;
    return s.substr(a, b - a);
}

// Removes an end-of-line comment: a '#' at the start or after whitespace, outside quotes.
std::string StripComment(const std::string& s) {
    char quote = 0;
    for (size_t i = 0; i < s.size(); i++) {
        const char c = s[i];
        if (quote) {
            if (c == quote) {
                if (quote == '\'' && i + 1 < s.size() && s[i + 1] == '\'') { i++; continue; }   // '' escape
                quote = 0;
            } else if (quote == '"' && c == '\\') {
                i++;
            }
            continue;
        }
        if (c == '"' || c == '\'') {
            // Quotes only open a quoted scalar at the start of a value, not inside a plain one (it's).
            if (i == 0 || s[i - 1] == ' ' || s[i - 1] == '\t' || s[i - 1] == ':' || s[i - 1] == '[' ||
                s[i - 1] == '{' || s[i - 1] == ',' || s[i - 1] == '-')
                quote = c;
            continue;
        }
        if (c == '#' && (i == 0 || s[i - 1] == ' ' || s[i - 1] == '\t')) return s.substr(0, i);
    }
    return s;
}

std::vector<Line> SplitLines(const std::string& text) {
    std::vector<Line> out;
    size_t pos = 0;
    if (text.size() >= 3 && (unsigned char)text[0] == 0xEF && (unsigned char)text[1] == 0xBB &&
        (unsigned char)text[2] == 0xBF)
        pos = 3;
    while (pos <= text.size()) {
        size_t nl = text.find('\n', pos);
        if (nl == std::string::npos) nl = text.size();
        std::string raw = text.substr(pos, nl - pos);
        pos = nl + 1;
        int indent = 0;
        size_t k = 0;
        while (k < raw.size() && (raw[k] == ' ' || raw[k] == '\t')) { indent += raw[k] == '\t' ? 2 : 1; k++; }
        std::string body = Trim(StripComment(raw.substr(k)));
        if (body.empty() || body == "---" || body == "...") {
            if (nl >= text.size()) break;
            continue;
        }
        out.push_back({ indent, body });
        if (nl >= text.size()) break;
    }
    return out;
}

std::string Unquote(const std::string& v) {
    if (v.size() >= 2 && v.front() == '\'' && v.back() == '\'') {
        std::string out;
        for (size_t i = 1; i + 1 < v.size(); i++) {
            out += v[i];
            if (v[i] == '\'' && i + 2 < v.size() && v[i + 1] == '\'') i++;
        }
        return out;
    }
    if (v.size() >= 2 && v.front() == '"' && v.back() == '"') {
        std::string out;
        for (size_t i = 1; i + 1 < v.size(); i++) {
            char c = v[i];
            if (c == '\\' && i + 2 < v.size()) {
                const char e = v[++i];
                switch (e) {
                    case 'n': out += '\n'; break;
                    case 't': out += '\t'; break;
                    case '\\': out += '\\'; break;
                    case '"': out += '"'; break;
                    case '/': out += '/'; break;
                    default: out += '\\'; out += e; break;
                }
            } else {
                out += c;
            }
        }
        return out;
    }
    return v;
}

YamlNode MakeScalar(const std::string& raw) {
    YamlNode n;
    const std::string v = Trim(raw);
    if (v.empty() || v == "~" || v == "null" || v == "Null" || v == "NULL") return n;   // Null
    n.kind = YamlNode::Scalar;
    n.scalar = Unquote(v);
    return n;
}

// Flow collections: { k: v, ... } and [ a, b ], nested; scalars as above.
struct Flow {
    const std::string& s;
    size_t i = 0;
    int depth = 0;
    void ws() { while (i < s.size() && (s[i] == ' ' || s[i] == '\t')) i++; }
    std::string token(const char* stops) {
        ws();
        size_t a = i;
        if (i < s.size() && (s[i] == '"' || s[i] == '\'')) {
            const char q = s[i++];
            while (i < s.size()) {
                if (s[i] == '\\' && q == '"') { i += 2; continue; }
                if (s[i] == q) { i++; break; }
                i++;
            }
            return s.substr(a, i - a);
        }
        while (i < s.size()) {
            const char c = s[i];
            bool stop = false;
            for (const char* p = stops; *p; p++) if (c == *p) stop = true;
            // ':' ends a key only when followed by a space or the end (URLs and times keep theirs).
            if (c == ':' && !(i + 1 >= s.size() || s[i + 1] == ' ')) stop = false;
            if (stop) break;
            i++;
        }
        return Trim(s.substr(a, i - a));
    }
    YamlNode value() {
        ws();
        if (++depth > 32) { i = s.size(); return YamlNode(); }
        YamlNode out;
        if (i < s.size() && s[i] == '{') {
            i++;
            out.kind = YamlNode::Map;
            while (i < s.size()) {
                ws();
                if (i < s.size() && s[i] == '}') { i++; break; }
                const std::string key = Unquote(token(":,}"));
                YamlNode v;
                ws();
                if (i < s.size() && s[i] == ':') { i++; v = value(); }
                out.map.emplace_back(key, std::move(v));
                ws();
                if (i < s.size() && s[i] == ',') { i++; continue; }
                if (i < s.size() && s[i] == '}') { i++; break; }
                if (i >= s.size()) break;
                i++;   // something unexpected: skip it
            }
        } else if (i < s.size() && s[i] == '[') {
            i++;
            out.kind = YamlNode::Seq;
            while (i < s.size()) {
                ws();
                if (i < s.size() && s[i] == ']') { i++; break; }
                out.seq.push_back(value());
                ws();
                if (i < s.size() && s[i] == ',') { i++; continue; }
                if (i < s.size() && s[i] == ']') { i++; break; }
                if (i >= s.size()) break;
                i++;
            }
        } else {
            out = MakeScalar(token(",}]"));
        }
        depth--;
        return out;
    }
};

YamlNode ParseValueText(const std::string& v) {
    if (!v.empty() && (v[0] == '{' || v[0] == '[')) {
        Flow f{ v };
        return f.value();
    }
    return MakeScalar(v);
}

// "key: value" / "key:" -> true with key and value text. A ':' must be followed by a space or the end.
bool SplitKey(const std::string& t, std::string& key, std::string& value) {
    size_t i = 0;
    if (!t.empty() && (t[0] == '"' || t[0] == '\'')) {
        const char q = t[0];
        i = 1;
        while (i < t.size() && t[i] != q) { if (t[i] == '\\' && q == '"') i++; i++; }
        if (i >= t.size()) return false;
        i++;
        size_t j = i;
        while (j < t.size() && t[j] == ' ') j++;
        if (j >= t.size() || t[j] != ':') return false;
        key = Unquote(t.substr(0, i));
        value = Trim(t.substr(j + 1));
        return true;
    }
    for (; i < t.size(); i++) {
        if (t[i] == ':' && (i + 1 == t.size() || t[i + 1] == ' ' || t[i + 1] == '\t')) {
            key = Trim(t.substr(0, i));
            value = Trim(t.substr(i + 1));
            return !key.empty();
        }
    }
    return false;
}

bool IsSeqItem(const std::string& t) { return t == "-" || (t.size() >= 2 && t[0] == '-' && t[1] == ' '); }

struct Parser {
    std::vector<Line> lines;
    size_t pos = 0;
    int depth = 0;

    // The block whose lines are indented at least `minIndent`; its own indent is the first line's.
    YamlNode block(int minIndent) {
        if (pos >= lines.size() || lines[pos].indent < minIndent || ++depth > 64) return YamlNode();
        YamlNode n = IsSeqItem(lines[pos].text) ? sequence(lines[pos].indent) : mapping(lines[pos].indent);
        depth--;
        return n;
    }

    // Raw text of a block scalar (| or >): the following lines indented deeper than `indent`.
    std::string blockScalar(int indent) {
        std::string out;
        while (pos < lines.size() && lines[pos].indent > indent) {
            if (!out.empty()) out += '\n';
            out += lines[pos++].text;
        }
        return out;
    }

    // A mapping value: inline text, else the deeper block (or a sequence at the same indent, which
    // YAML allows directly under a key).
    YamlNode valueAfterKey(const std::string& v, int indent) {
        if (v == "|" || v == ">" || v == "|-" || v == ">-") {
            YamlNode n;
            n.kind = YamlNode::Scalar;
            n.scalar = blockScalar(indent);
            return n;
        }
        if (!v.empty()) return ParseValueText(v);
        if (pos < lines.size() && lines[pos].indent > indent) return block(indent + 1);
        if (pos < lines.size() && lines[pos].indent == indent && IsSeqItem(lines[pos].text)) return sequence(indent);
        return YamlNode();
    }

    YamlNode mapping(int indent) {
        YamlNode n;
        n.kind = YamlNode::Map;
        while (pos < lines.size()) {
            const Line& ln = lines[pos];
            if (ln.indent < indent) break;
            if (ln.indent > indent || IsSeqItem(ln.text)) {
                if (ln.indent == indent) break;   // a sequence at this indent ends the mapping
                pos++;                            // stray deeper line: skip
                continue;
            }
            std::string key, v;
            if (!SplitKey(ln.text, key, v)) { pos++; continue; }
            pos++;
            n.map.emplace_back(key, valueAfterKey(v, indent));
        }
        return n;
    }

    YamlNode sequence(int indent) {
        YamlNode n;
        n.kind = YamlNode::Seq;
        while (pos < lines.size()) {
            Line& ln = lines[pos];
            if (ln.indent != indent || !IsSeqItem(ln.text)) {
                if (ln.indent > indent) { pos++; continue; }   // stray deeper line
                break;
            }
            const std::string rest = ln.text.size() > 1 ? Trim(ln.text.substr(1)) : std::string();
            if (rest.empty()) {
                pos++;
                n.seq.push_back(pos < lines.size() && lines[pos].indent > indent ? block(indent + 1) : YamlNode());
                continue;
            }
            std::string key, v;
            if (rest[0] != '{' && rest[0] != '[' && SplitKey(rest, key, v)) {
                // "- key: value": a mapping whose keys sit at the column after "- ".
                const int col = indent + (int)(ln.text.size() - rest.size());
                ln.indent = col;
                ln.text = rest;
                n.seq.push_back(mapping(col));
                continue;
            }
            pos++;
            n.seq.push_back(ParseValueText(rest));
        }
        return n;
    }
};

}  // namespace

const YamlNode* YamlNode::find(const std::string& key) const {
    if (kind != Map) return nullptr;
    for (const auto& kv : map)
        if (kv.first == key) return &kv.second;
    return nullptr;
}

std::string YamlNode::str(const std::string& key) const {
    const YamlNode* n = find(key);
    return n && n->kind == Scalar ? n->scalar : std::string();
}

int YamlNode::integer(const std::string& key, int def) const {
    const std::string s = str(key);
    if (s.empty()) return def;
    char* end = nullptr;
    const long v = std::strtol(s.c_str(), &end, 10);
    return end && *end == '\0' ? (int)v : def;
}

bool YamlNode::isNullish() const {
    return kind == Null || (kind == Scalar && (scalar.empty() || scalar == "null" || scalar == "~"));
}

YamlNode ParseYaml(const std::string& text) {
    Parser p;
    p.lines = SplitLines(text);
    YamlNode root = p.block(0);
    // Anything after the first top-level block at indent 0 (a second mapping run after a stray line)
    // is merged in, so one odd line never hides the rest of the file.
    while (p.pos < p.lines.size()) {
        const size_t before = p.pos;
        YamlNode more = p.block(0);
        if (root.kind == YamlNode::Map && more.kind == YamlNode::Map)
            for (auto& kv : more.map) root.map.push_back(std::move(kv));
        if (p.pos == before) p.pos++;
    }
    return root;
}

}  // namespace dlc
