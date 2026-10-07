#include "doctest.h"
#include "ipc_json.h"
#include <string>

// ============================================================================
// Calibration-pipe JSON parser (src/ipc_json.h). The pipe accepts requests from any
// process that can connect, so the parser must refuse hostile input with an error —
// never crash the 24/7 host (a stack overflow is not a C++ exception).
// ============================================================================

using namespace ipc_json;

namespace {
bool Parses(const std::string& text, JsonValue* out = nullptr) {
    try {
        JsonParser p(text);
        JsonValue v = p.parse();
        if (out) *out = std::move(v);
        return true;
    } catch (const std::exception&) {
        return false;
    }
}
std::string ErrorOf(const std::string& text) {
    try {
        JsonParser p(text);
        p.parse();
        return "";
    } catch (const std::exception& e) {
        return e.what();
    }
}
}  // namespace

TEST_CASE("IPC JSON: a typical request parses") {
    JsonValue v;
    REQUIRE(Parses(R"({"method":"mhc.grayscale_set_live","params":{"monitor":0,"mode":"SDR","points":[0,0.25,1e-3,-2.5E2]}})", &v));
    CHECK(v.type == JsonValue::Obj);
    CHECK(v.getStr("method") == "mhc.grayscale_set_live");
    const JsonValue* params = v.find("params");
    REQUIRE(params);
    CHECK(params->getInt("monitor") == 0);
    const JsonValue* pts = params->find("points");
    REQUIRE(pts);
    REQUIRE(pts->arr.size() == 4);
    CHECK(pts->arr[1].num == 0.25);
    CHECK(pts->arr[2].num == 0.001);
    CHECK(pts->arr[3].num == -250.0);
}

TEST_CASE("IPC JSON: deep nesting is refused, not recursed into") {
    // 256 KiB of '[' used to overflow the pipe thread's stack and abort the process.
    std::string bomb(256 * 1024, '[');
    CHECK(ErrorOf(bomb) == "nesting too deep");
    std::string objBomb;
    for (int k = 0; k < 10000; k++) objBomb += "{\"a\":";
    CHECK(ErrorOf(objBomb) == "nesting too deep");

    // The cap itself: kMaxDepth levels parse, one more does not.
    std::string ok(kMaxDepth, '['), okClose(kMaxDepth, ']');
    CHECK(Parses(ok + okClose));
    std::string over(kMaxDepth + 1, '['), overClose(kMaxDepth + 1, ']');
    CHECK(ErrorOf(over + overClose) == "nesting too deep");
}

TEST_CASE("IPC JSON: the value count is capped") {
    std::string many = "[";
    for (size_t k = 0; k < kMaxValues + 10; k++) { if (k) many += ','; many += '0'; }
    many += ']';
    CHECK(ErrorOf(many) == "too many values");
}

TEST_CASE("IPC JSON: malformed and out-of-range numbers are rejected") {
    CHECK_FALSE(Parses("[1e400]"));     // would be inf
    CHECK_FALSE(Parses("[-1e400]"));
    CHECK_FALSE(Parses("[1.2.3]"));
    CHECK_FALSE(Parses("[-]"));
    CHECK_FALSE(Parses("[1e]"));
    CHECK_FALSE(Parses("[--1]"));
    CHECK(Parses("[1e308]"));
    CHECK(Parses("[0.000001]"));
}

TEST_CASE("IPC JSON: trailing characters and truncation are errors") {
    CHECK_FALSE(Parses("{} garbage"));
    CHECK_FALSE(Parses("{}{}"));
    CHECK(Parses("{}\r\n"));
    CHECK_FALSE(Parses("{\"a\":1"));
    CHECK_FALSE(Parses("[1,2"));
    CHECK_FALSE(Parses("{\"a\""));
    CHECK_FALSE(Parses("\"unterminated"));
    CHECK_FALSE(Parses(""));
    CHECK_FALSE(Parses("{\"a\":tru}"));
}

TEST_CASE("IPC JSON: string escapes and UTF-8") {
    // JSON text: {"p":"C:\\luts\\a.cube","u":"\u00e9\ud83d\ude00"}
    const std::string text = "{\"p\":\"C:\\\\luts\\\\a.cube\",\"u\":\"\\u00e9\\ud83d\\ude00\"}";
    const std::string wantPath = "C:\\luts\\a.cube";
    const std::string wantUtf8 = "\xc3\xa9\xf0\x9f\x98\x80";   // e-acute + U+1F600
    JsonValue v;
    REQUIRE(Parses(text, &v));
    CHECK(v.getStr("p") == wantPath);
    CHECK(v.getStr("u") == wantUtf8);
}

TEST_CASE("IPC JSON: serialization round-trips and never emits inf/nan") {
    const std::string text = "{\"a\":[true,false,null,1.5,\"x\\\"y\"],\"b\":{}}";
    JsonValue v;
    REQUIRE(Parses(text, &v));
    std::string out;
    Serialize(v, out);
    CHECK(out == text);

    JsonValue o = JObj();
    o.set("inf", JNum(HUGE_VAL));
    o.set("nan", JNum(std::nan("")));
    std::string s;
    Serialize(o, s);
    const std::string wantNulls = "{\"inf\":null,\"nan\":null}";
    CHECK(s == wantNulls);
    CHECK(Parses(s));
}
