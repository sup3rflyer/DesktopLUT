#include "doctest.h"
#include "actions/logcap.h"
#include "actions/notify.h"
#include "fald_core_status.h"   // FaldCoreGovPostEvent / FaldCoreGovLayersStopped: the rate-cap condition
#include "ui/ui_views.h"        // ui::FooterStamp
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
// The footer's headline (notify::PickHeadline / Headline): a warning stands until its condition is resolved or the
// user has seen it (the Events page), else the newest "status" line. Nothing is removed from the ring for it.
// ============================================================================

namespace {
// The live footer pick's title, "" = none.
std::wstring HeadTitle() {
    notify::Event e;
    return notify::Headline(e) ? e.title : std::wstring();
}
notify::Event Ev(uint64_t seq, notify::Sev sev, int mon, const wchar_t* src, const wchar_t* title, uint64_t resolved = 0) {
    notify::Event e;
    e.seq = seq;
    e.sev = sev;
    e.monitor = mon;
    e.source = src;
    e.title = title;
    e.resolvedSeq = resolved;
    return e;
}
const wchar_t* kCap = L"FALD rate-capped: GPU over budget";
}  // namespace

TEST_CASE("headline: the pure pick over a list") {
    using notify::Sev;
    std::vector<notify::Event> v;
    CHECK(notify::PickHeadline(v, 0) == nullptr);
    v.push_back(Ev(1, Sev::Info, -1, L"status", L"Active"));
    REQUIRE(notify::PickHeadline(v, 0) != nullptr);
    CHECK(notify::PickHeadline(v, 0)->title == L"Active");
    CHECK(notify::PickHeadline(v, 99)->title == L"Active");          // a status line is not acknowledged away
    v.push_back(Ev(2, Sev::Warning, 0, L"fald", kCap));
    v.push_back(Ev(3, Sev::Info, -1, L"status", L"Active (DWM Hook)"));
    CHECK(notify::PickHeadline(v, 0)->title == kCap);                 // an unseen warning beats a newer status line
    CHECK(notify::PickHeadline(v, 2)->title == L"Active (DWM Hook)"); // seen: the newest status line
    v[1].resolvedSeq = 3;
    CHECK(notify::PickHeadline(v, 0)->title == L"Active (DWM Hook)"); // resolved: the same
    v.push_back(Ev(4, Sev::Success, 0, L"mhc", L"Profile installed"));
    v.push_back(Ev(5, Sev::Info, 0, L"hook", L"Injected"));
    CHECK(notify::PickHeadline(v, 0)->title == L"Active (DWM Hook)"); // other sources' info / success never show
    v.push_back(Ev(6, Sev::Error, -1, L"ui", L"An interface action failed"));
    CHECK(notify::PickHeadline(v, 0)->title == L"An interface action failed");   // an error is a warning here
    CHECK(notify::PickHeadline(v, 6)->title == L"Active (DWM Hook)");
}

TEST_CASE("headline: a condition warning ends with its resolving event") {
    notify::ResetForTest();
    notify::Post(notify::Sev::Info, -1, L"status", L"Active (DWM Hook)");
    notify::PostCondition(notify::Sev::Warning, 0, L"fald", L"rate-cap", kCap, L"HDR");
    CHECK(HeadTitle() == kCap);
    notify::Resolve(0, L"fald", L"rate-cap");
    notify::Post(notify::Sev::Info, 0, L"fald", L"FALD full rate resumed", L"HDR");
    CHECK(HeadTitle() == L"Active (DWM Hook)");
    // Events keeps the whole history: the warning is still there, marked resolved.
    auto ev = notify::Snapshot();
    REQUIRE(ev.size() == 3);
    CHECK(ev[1].title == kCap);
    CHECK(ev[1].resolvedSeq == 2);
    CHECK(ev[2].resolvedSeq == 0);
}

TEST_CASE("headline: no status line at all and the warning resolved: nothing") {
    notify::ResetForTest();
    notify::PostCondition(notify::Sev::Warning, 0, L"fald", L"rate-cap", kCap);
    notify::Resolve(0, L"fald", L"rate-cap");
    notify::Post(notify::Sev::Info, 0, L"fald", L"FALD full rate resumed");
    CHECK(HeadTitle().empty());
}

TEST_CASE("headline: an unrelated event never ends a warning") {
    notify::ResetForTest();
    notify::PostCondition(notify::Sev::Warning, 0, L"fald", L"rate-cap", kCap);
    notify::Post(notify::Sev::Info, 0, L"mhc", L"Refresh trim 60 Hz");
    notify::Post(notify::Sev::Info, -1, L"status", L"Active (DWM Hook)");
    notify::Resolve(0, L"mhc", L"rate-cap");            // another source, same key
    notify::Resolve(0, L"fald", L"panel 0,0 hdr");      // the same source, another condition
    notify::Resolve(1, L"fald", L"rate-cap");           // the same condition on another display
    CHECK(HeadTitle() == kCap);
}

TEST_CASE("headline: two displays capped, one resumes: the other's warning stands") {
    notify::ResetForTest();
    notify::PostCondition(notify::Sev::Warning, 0, L"fald", L"rate-cap", L"capped 0");
    notify::PostCondition(notify::Sev::Warning, 1, L"fald", L"rate-cap", L"capped 1");
    CHECK(HeadTitle() == L"capped 1");
    notify::Resolve(1, L"fald", L"rate-cap");
    notify::Post(notify::Sev::Info, 1, L"fald", L"FALD full rate resumed");
    CHECK(HeadTitle() == L"capped 0");
    notify::Resolve(notify::kAnyMonitor, L"fald", L"rate-cap");   // the live layers stopped
    CHECK(HeadTitle().empty());
}

TEST_CASE("headline: the FALD governor's own events (FaldCoreGovPostEvent)") {
    notify::ResetForTest();
    FaldCoreGovReport capped;
    capped.state = FaldCoreGovState::Capped;
    capped.p90Ms = 3.1; capped.budgetMs = 2.5; capped.spacingMs = 33;
    FaldCoreGovReport running;
    running.state = FaldCoreGovState::Running;
    FaldCoreGovReport forced;
    forced.state = FaldCoreGovState::Forced;
    notify::Post(notify::Sev::Info, -1, L"status", L"Active (DWM Hook)");
    FaldCoreGovPostEvent(0, true, capped);
    FaldCoreGovPostEvent(1, true, capped);
    CHECK(HeadTitle() == kCap);
    FaldCoreGovPostEvent(1, true, running);   // display 1 resumes: display 0 is still capped
    CHECK(HeadTitle() == kCap);
    FaldCoreGovPostEvent(0, true, forced);    // display 0's budget forced off: over too
    CHECK(HeadTitle() == L"Active (DWM Hook)");
    FaldCoreGovPostEvent(0, true, capped);    // capped again: a new warning
    CHECK(HeadTitle() == kCap);
    FaldCoreGovLayersStopped();               // the live layers stopped: every cap is over
    CHECK(HeadTitle() == L"Active (DWM Hook)");
}

TEST_CASE("headline: a resolve with nothing to end changes nothing; a clean pass ends every display's condition") {
    notify::ResetForTest();
    notify::Post(notify::Sev::Info, -1, L"status", L"Active (DWM Hook)");
    notify::PostCondition(notify::Sev::Warning, 0, L"mhc", L"refresh-trim", L"Refresh trim swap failed");
    notify::PostCondition(notify::Sev::Warning, -1, L"mhc", L"refresh-trim", L"Refresh trim could not be applied");
    const uint64_t rev = notify::Revision();
    notify::Resolve(-1, L"mhc", L"windows-tonemap");                  // another condition
    notify::Resolve(3, L"mhc", L"refresh-trim");                      // another display
    notify::Resolve(notify::kAnyMonitor, L"fald", L"refresh-trim");   // another source
    CHECK(notify::Revision() == rev);                                 // a periodic pass that ends nothing wakes nobody
    CHECK(HeadTitle() == L"Refresh trim could not be applied");
    notify::Resolve(notify::kAnyMonitor, L"mhc", L"refresh-trim");    // a pass with no display failing: both are over
    CHECK(notify::Revision() != rev);
    CHECK(HeadTitle() == L"Active (DWM Hook)");
    const uint64_t rev2 = notify::Revision();
    notify::Resolve(notify::kAnyMonitor, L"mhc", L"refresh-trim");    // again: nothing left to end
    CHECK(notify::Revision() == rev2);
}

TEST_CASE("headline: a one-off warning stands until it is seen; a newer one shows again") {
    notify::ResetForTest();
    notify::Post(notify::Sev::Info, -1, L"status", L"Active (DWM Hook)");
    notify::Post(notify::Sev::Warning, -1, L"hotkey", L"Analysis hotkey Win+Shift+A is taken by another program");
    notify::Post(notify::Sev::Info, -1, L"status", L"Applied");
    CHECK(HeadTitle() == L"Analysis hotkey Win+Shift+A is taken by another program");   // a newer status line: still
    const uint64_t rev = notify::Revision();
    CHECK(notify::Acknowledge(notify::LastSeq()));        // the Events page opened
    CHECK(notify::Revision() != rev);
    CHECK_FALSE(notify::Acknowledge(notify::LastSeq()));  // nothing new: no change
    CHECK_FALSE(notify::Acknowledge(1));                  // never moves back
    CHECK(notify::Acknowledged() == 3);
    CHECK(HeadTitle() == L"Applied");
    notify::Post(notify::Sev::Error, 0, L"mhc", L"Failed to install MHC2 profile");
    CHECK(HeadTitle() == L"Failed to install MHC2 profile");
    // A resolve of a one-off (no condition key) changes nothing: only being seen ends it.
    notify::Resolve(0, L"mhc", nullptr);
    CHECK(HeadTitle() == L"Failed to install MHC2 profile");
    notify::Acknowledge(notify::LastSeq());
    CHECK(HeadTitle() == L"Applied");
    // An acknowledged condition stays acknowledged while it stands (the user has seen it).
    notify::PostCondition(notify::Sev::Warning, 0, L"fald", L"rate-cap", kCap);
    notify::Acknowledge(notify::LastSeq());
    CHECK(HeadTitle() == L"Applied");
}

TEST_CASE("headline: a newer condition of the same key replaces the older; one resolve ends both") {
    notify::ResetForTest();
    notify::PostCondition(notify::Sev::Warning, -1, L"hook", L"lut-read", L"first list");
    notify::PostCondition(notify::Sev::Warning, -1, L"hook", L"lut-read", L"second list");
    CHECK(HeadTitle() == L"second list");
    notify::Resolve(-1, L"hook", L"lut-read");
    CHECK(HeadTitle().empty());
    auto ev = notify::Snapshot();
    REQUIRE(ev.size() == 2);
    CHECK(ev[0].resolvedSeq == 2);
    CHECK(ev[1].resolvedSeq == 2);
}

TEST_CASE("headline: Resolve tells the listener only when it changed something") {
    notify::ResetForTest();
    notify::PostCondition(notify::Sev::Warning, 0, L"fald", L"rate-cap", kCap);
    g_listenerCalls = 0;
    notify::SetListener(&CountingListener);
    notify::Resolve(1, L"fald", L"rate-cap");   // nothing on display 1
    CHECK(g_listenerCalls.load() == 0);
    const uint64_t rev = notify::Revision();
    notify::Resolve(0, L"fald", L"rate-cap");
    CHECK(g_listenerCalls.load() == 1);
    CHECK(notify::Revision() != rev);
    notify::Resolve(0, L"fald", L"rate-cap");   // already resolved
    CHECK(g_listenerCalls.load() == 1);
    notify::SetListener(nullptr);
}

TEST_CASE("headline: the walk covers the whole ring; what the ring dropped never comes back") {
    notify::ResetForTest();
    notify::Post(notify::Sev::Info, -1, L"status", L"Active (DWM Hook)");
    notify::PostCondition(notify::Sev::Warning, 0, L"fald", L"rate-cap", kCap);
    // Far more than the old walk's 32 unrelated events: a live condition still stands.
    for (int i = 0; i < 200; i++) notify::Post(notify::Sev::Info, 0, L"mhc", L"unrelated");
    CHECK(HeadTitle() == kCap);
    // An old, resolved warning that later scrolls out never resurrects; the newest status line within the ring shows.
    notify::Resolve(0, L"fald", L"rate-cap");
    for (size_t i = 0; i < notify::kCapacity; i++) notify::Post(notify::Sev::Info, 0, L"mhc", L"unrelated");
    CHECK(HeadTitle().empty());   // the status line scrolled out too (Events no longer has it either)
    notify::Post(notify::Sev::Info, -1, L"status", L"Applied");
    CHECK(HeadTitle() == L"Applied");
    // The bound: a condition warning older than the whole ring is gone from Events and from the footer alike.
    notify::PostCondition(notify::Sev::Warning, 1, L"fald", L"rate-cap", L"capped 1");
    for (size_t i = 0; i < notify::kCapacity; i++) notify::Post(notify::Sev::Info, 0, L"mhc", L"unrelated");
    CHECK(HeadTitle().empty());
    // A resolve after the event left the ring is harmless.
    notify::Resolve(1, L"fald", L"rate-cap");
    CHECK(HeadTitle().empty());
}

TEST_CASE("headline: the footer's time stamp (ui::FooterStamp)") {
    SYSTEMTIME t = {};
    t.wYear = 2026; t.wMonth = 11; t.wDay = 14; t.wHour = 13; t.wMinute = 2; t.wSecond = 59;
    SYSTEMTIME now = t;
    now.wHour = 16;
    CHECK(ui::FooterStamp(t, now) == "13:02  ");
    now.wDay = 15;   // the warning is from yesterday: the date says so
    CHECK(ui::FooterStamp(t, now) == "11-14 13:02  ");
    now = t; now.wYear = 2027;
    CHECK(ui::FooterStamp(t, now) == "11-14 13:02  ");
}

TEST_CASE("headline: posting from many threads while resolving and picking") {
    notify::ResetForTest();
    std::atomic<bool> stop{ false };
    std::thread reader([&] {
        while (!stop.load()) { notify::Event e; notify::Headline(e); notify::Acknowledge(notify::LastSeq() / 2); }
    });
    std::vector<std::thread> threads;
    for (int t = 0; t < 4; t++)
        threads.emplace_back([t] {
            for (int i = 0; i < 300; i++) {
                notify::PostCondition(notify::Sev::Warning, t, L"fald", L"rate-cap", L"c");
                notify::Resolve(t, L"fald", L"rate-cap");
            }
        });
    for (auto& th : threads) th.join();
    stop = true;
    reader.join();
    CHECK(notify::LastSeq() == 1200);
    for (const notify::Event& e : notify::Snapshot()) CHECK(e.resolvedSeq >= e.seq);
    notify::Event e;
    CHECK_FALSE(notify::Headline(e));
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
