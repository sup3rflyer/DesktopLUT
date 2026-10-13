#include "actions/logcap.h"

#include <atomic>
#include <deque>
#include <iostream>
#include <mutex>

namespace logcap {

namespace {

std::mutex g_mutex;
std::deque<std::shared_ptr<const Line>> g_ring;
uint64_t g_seq = 0;
std::atomic<void (*)()> g_listener{ nullptr };

std::string Utf8(const std::wstring& w) {
    if (w.empty()) return {};
    const int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string s((size_t)(n > 0 ? n : 0), '\0');
    if (n > 0) WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), s.data(), n, nullptr, nullptr);
    return s;
}

}  // namespace

void Append(const std::string& utf8, bool err) {
    auto l = std::make_shared<Line>();   // built outside the lock
    GetLocalTime(&l->time);
    l->err = err;
    l->text = utf8;
    std::shared_ptr<const Line> dropped;   // freed outside the lock
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        l->seq = ++g_seq;
        g_ring.push_back(std::move(l));
        if (g_ring.size() > kCapacity) {
            dropped = std::move(g_ring.front());
            g_ring.pop_front();
        }
    }
    if (auto fn = g_listener.load()) fn();
}

void Install() {
    static bool installed = false;
    if (installed) return;
    installed = true;
    // Never destroyed: the streams may be written during static destruction.
    auto narrow = [](const std::string& s, bool err) { Append(s, err); };
    auto wide = [](const std::wstring& s, bool err) { Append(Utf8(s), err); };
    std::cout.rdbuf(new TeeBuf<char>(std::cout.rdbuf(), false, narrow));
    std::cerr.rdbuf(new TeeBuf<char>(std::cerr.rdbuf(), true, narrow));
    std::wcout.rdbuf(new TeeBuf<wchar_t>(std::wcout.rdbuf(), false, wide));
    std::wcerr.rdbuf(new TeeBuf<wchar_t>(std::wcerr.rdbuf(), true, wide));
}

std::vector<std::shared_ptr<const Line>> Snapshot(size_t max) {
    std::lock_guard<std::mutex> lock(g_mutex);
    const size_t n = g_ring.size() < max ? g_ring.size() : max;
    return std::vector<std::shared_ptr<const Line>>(g_ring.end() - (std::ptrdiff_t)n, g_ring.end());
}

uint64_t LastSeq() {
    std::lock_guard<std::mutex> lock(g_mutex);
    return g_seq;
}

void SetListener(void (*fn)()) { g_listener.store(fn); }

void ResetForTest() {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_ring.clear();
    g_seq = 0;
}

}  // namespace logcap
