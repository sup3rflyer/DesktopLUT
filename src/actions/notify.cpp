#include "notify.h"

#include <atomic>
#include <deque>
#include <mutex>

namespace notify {

namespace {

std::mutex g_mutex;
std::deque<Event> g_ring;          // guarded by g_mutex
uint64_t g_seq = 0;                // guarded by g_mutex
std::atomic<uint64_t> g_lastSeq{0};
std::atomic<Listener> g_listener{nullptr};

}  // namespace

void Post(Sev sev, int monitor, const wchar_t* source, std::wstring title, std::wstring detail) {
    Event e;
    GetLocalTime(&e.time);
    e.sev = sev;
    e.monitor = monitor;
    e.source = source ? source : L"";
    e.title = std::move(title);
    e.detail = std::move(detail);
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        e.seq = ++g_seq;
        g_ring.push_back(std::move(e));
        while (g_ring.size() > kCapacity) g_ring.pop_front();
        g_lastSeq.store(g_seq, std::memory_order_release);
    }
    if (Listener fn = g_listener.load(std::memory_order_acquire)) fn();
}

std::vector<Event> Snapshot(size_t max) {
    std::lock_guard<std::mutex> lock(g_mutex);
    size_t n = g_ring.size() < max ? g_ring.size() : max;
    return std::vector<Event>(g_ring.end() - (ptrdiff_t)n, g_ring.end());
}

uint64_t LastSeq() { return g_lastSeq.load(std::memory_order_acquire); }

void SetListener(Listener fn) { g_listener.store(fn, std::memory_order_release); }

void ResetForTest() {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_ring.clear();
    g_seq = 0;
    g_lastSeq.store(0, std::memory_order_release);
}

}  // namespace notify
