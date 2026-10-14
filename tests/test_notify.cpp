#include "doctest.h"
#include "actions/logcap.h"
#include "actions/notify.h"
#include <sstream>
#include <atomic>
#include <thread>
#include <vector>

// ============================================================================
// Notification log (src/actions/notify.*): the one history the classic status bar and the new
// UI's Events page both read. SetStatus also posts here, from the render thread among others.
// ============================================================================

namespace {
std::atomic<int> g_listenerCalls{0};
void CountingListener() { g_listenerCalls.fetch_add(1); }
}  // namespace

TEST_CASE("notify: events keep order, sequence and fields") {
    notify::ResetForTest();
    CHECK(notify::LastSeq() == 0);
    notify::Post(notify::Sev::Info, -1, L"status", L"Active");
    notify::Post(notify::Sev::Warning, 1, L"mhc", L"Profile restored", L"Windows dropped it");
    auto ev = notify::Snapshot();
    REQUIRE(ev.size() == 2);
    CHECK(ev[0].seq == 1);
    CHECK(ev[1].seq == 2);
    CHECK(ev[0].title == L"Active");
    CHECK(ev[0].monitor == -1);
    CHECK(ev[1].sev == notify::Sev::Warning);
    CHECK(ev[1].monitor == 1);
    CHECK(ev[1].source == L"mhc");
    CHECK(ev[1].detail == L"Windows dropped it");
    CHECK(notify::LastSeq() == 2);
}

TEST_CASE("notify: the ring is bounded and keeps the newest events") {
    notify::ResetForTest();
    const size_t total = notify::kCapacity + 37;
    for (size_t i = 0; i < total; i++) notify::Post(notify::Sev::Info, -1, L"t", std::to_wstring(i));
    auto ev = notify::Snapshot();
    REQUIRE(ev.size() == notify::kCapacity);
    CHECK(ev.front().title == std::to_wstring(total - notify::kCapacity));
    CHECK(ev.back().title == std::to_wstring(total - 1));
    CHECK(notify::LastSeq() == total);
    auto last3 = notify::Snapshot(3);
    REQUIRE(last3.size() == 3);
    CHECK(last3.back().seq == total);
}

TEST_CASE("notify: the listener runs once per post, from any thread") {
    notify::ResetForTest();
    g_listenerCalls = 0;
    notify::SetListener(&CountingListener);
    std::vector<std::thread> threads;
    for (int t = 0; t < 4; t++)
        threads.emplace_back([] { for (int i = 0; i < 50; i++) notify::Post(notify::Sev::Info, -1, L"t", L"x"); });
    for (auto& th : threads) th.join();
    notify::SetListener(nullptr);
    CHECK(g_listenerCalls.load() == 200);
    CHECK(notify::LastSeq() == 200);
    auto ev = notify::Snapshot();
    for (size_t i = 1; i < ev.size(); i++) CHECK(ev[i].seq == ev[i - 1].seq + 1);
}

// ============================================================================
// Diagnostic log capture (src/actions/logcap.*): what std::cout / cerr write, as lines, still forwarded.
// ============================================================================

TEST_CASE("logcap: a tee buffer forwards everything and hands over whole lines") {
    std::ostringstream console;
    std::vector<std::pair<std::string, bool>> lines;
    logcap::TeeBuf<char> tee(console.rdbuf(), true, [&](const std::string& l, bool err) { lines.emplace_back(l, err); });
    std::ostream out(&tee);
    out << "[Tag] first " << 42 << '\n' << "second" << std::endl << "partial";
    CHECK(console.str() == "[Tag] first 42\nsecond\npartial");   // the console sees it unchanged
    REQUIRE(lines.size() == 2);                                    // "partial" waits for its line break
    CHECK(lines[0].first == "[Tag] first 42");
    CHECK(lines[0].second);
    CHECK(lines[1].first == "second");
    out << " done\r\n";
    REQUIRE(lines.size() == 3);
    CHECK(lines[2].first == "partial done");                       // CR stripped
}

namespace {
// What the original std::cout / std::wcout buffer is in a GUI process without a console: every write and every
// flush fails (the standard handles are invalid).
template <class Ch>
struct DeadBuf : std::basic_streambuf<Ch> {
    using Traits = typename std::basic_streambuf<Ch>::traits_type;
    int syncs = 0;
    typename Traits::int_type overflow(typename Traits::int_type) override { return Traits::eof(); }
    std::streamsize xsputn(const Ch*, std::streamsize) override { return 0; }
    int sync() override { syncs++; return -1; }
};
}  // namespace

TEST_CASE("logcap: a failing forward (no console) never breaks the stream; every line is still captured") {
    // 2026-10-14: sync() passed the dead stdout's -1 on, so the first std::endl set badbit and the stream dropped
    // every later line (the Events log kept one line per stream for a whole session).
    {
        DeadBuf<char> dead;
        std::vector<std::string> lines;
        logcap::TeeBuf<char> tee(&dead, false, [&](const std::string& l, bool) { lines.push_back(l); });
        std::ostream out(&tee);
        for (int i = 0; i < 5; i++) out << "[Tag] line " << i << std::endl;
        CHECK(out.good());
        CHECK(dead.syncs == 5);   // the forward is still flushed (a console attached later works)
        REQUIRE(lines.size() == 5);
        CHECK(lines[4] == "[Tag] line 4");
    }
    {
        DeadBuf<wchar_t> dead;
        std::vector<std::wstring> lines;
        logcap::TeeBuf<wchar_t> tee(&dead, true, [&](const std::wstring& l, bool) { lines.push_back(l); });
        std::wostream out(&tee);
        for (int i = 0; i < 5; i++) out << L"[Wide] line " << i << std::endl;
        out.flush();
        CHECK(out.good());
        REQUIRE(lines.size() == 5);
        CHECK(lines[0] == L"[Wide] line 0");
    }
}

TEST_CASE("logcap: lines from two threads never interleave; the ring is bounded") {
    logcap::ResetForTest();
    logcap::TeeBuf<char> tee(nullptr, false, [](const std::string& l, bool err) { logcap::Append(l, err); });
    auto writer = [&](char c) {   // one ostream per thread over the shared buffer (as std::cout's threads share it)
        std::ostream out(&tee);
        for (int i = 0; i < 300; i++) out << std::string(5, c) << i << std::string(5, c) << '\n';
    };
    std::thread a(writer, 'a'), b(writer, 'b');
    a.join();
    b.join();
    const auto lines = logcap::Snapshot();
    CHECK(lines.size() == 600);
    for (const auto& l : lines) {
        const char c = l->text.front();
        CHECK(l->text.find(c == 'a' ? 'b' : 'a') == std::string::npos);
    }
    CHECK(lines.back()->seq == logcap::LastSeq());
    for (size_t i = 0; i < logcap::kCapacity + 10; i++) logcap::Append("x", false);
    CHECK(logcap::Snapshot().size() == logcap::kCapacity);
    logcap::ResetForTest();
}
