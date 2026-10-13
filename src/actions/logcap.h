#pragma once
// The diagnostic log: every line written to std::cout / std::cerr / std::wcout / std::wcerr, kept in a
// bounded ring for the Events page, and still forwarded to the console when one is open. Lines are
// assembled per thread, so two threads writing at once never interleave inside a line.
//
// Install() once, early, on the GUI thread (before other threads start writing). The ring and the
// stream buffers live until the process exits.

#include <windows.h>
#include <cstdint>
#include <functional>
#include <memory>
#include <streambuf>
#include <string>
#include <vector>

namespace logcap {

struct Line {
    uint64_t seq = 0;          // 1-based, monotonically increasing
    SYSTEMTIME time = {};      // local time the line was completed
    bool err = false;          // written to cerr / wcerr
    std::string text;          // UTF-8, without the line break
};

constexpr size_t kCapacity = 4000;

void Install();
// Oldest first. Lines are shared and immutable: a snapshot copies pointers, so the writers (render and
// processing threads among them) never wait on a copy of thousands of strings.
std::vector<std::shared_ptr<const Line>> Snapshot(size_t max = kCapacity);
uint64_t LastSeq();
void SetListener(void (*fn)());                        // called after each line (any thread; must not block)

// ---- exposed for tests ---------------------------------------------------------------------------
// A stream buffer that forwards everything to `forward` (may be null) and hands each completed line to
// `sink`. Unbuffered: every write reaches it, so per-thread line assembly is exact.
template <class Ch>
class TeeBuf : public std::basic_streambuf<Ch> {
public:
    using Sink = std::function<void(const std::basic_string<Ch>&, bool err)>;
    TeeBuf(std::basic_streambuf<Ch>* forward, bool err, Sink sink) : m_forward(forward), m_err(err), m_sink(std::move(sink)) {}
    std::basic_streambuf<Ch>* forward() const { return m_forward; }

protected:
    using Traits = typename std::basic_streambuf<Ch>::traits_type;
    using Int = typename Traits::int_type;
    Int overflow(Int ch) override {
        if (Traits::eq_int_type(ch, Traits::eof())) return Traits::not_eof(ch);
        const Ch c = Traits::to_char_type(ch);
        if (m_forward) m_forward->sputc(c);
        Feed(&c, 1);
        return ch;
    }
    std::streamsize xsputn(const Ch* s, std::streamsize n) override {
        if (m_forward) m_forward->sputn(s, n);
        Feed(s, n);
        return n;
    }
    int sync() override { return m_forward ? m_forward->pubsync() : 0; }

private:
    void Feed(const Ch* s, std::streamsize n) {
        thread_local std::basic_string<Ch> pending[2];   // [err]: one partial line per thread and stream
        std::basic_string<Ch>& line = pending[m_err ? 1 : 0];
        for (std::streamsize i = 0; i < n; i++) {
            if (s[i] == Ch('\n')) {
                if (!line.empty() && line.back() == Ch('\r')) line.pop_back();
                if (m_sink) m_sink(line, m_err);
                line.clear();
            } else if (line.size() < 4096) {   // a runaway line without a break is capped, not grown
                line += s[i];
            }
        }
    }
    std::basic_streambuf<Ch>* m_forward;
    bool m_err;
    Sink m_sink;
};

// Appends one line to the ring (what the installed buffers call).
void Append(const std::string& utf8, bool err);
void ResetForTest();

}  // namespace logcap
