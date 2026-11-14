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
std::atomic<uint64_t> g_revision{0};
std::atomic<uint64_t> g_acknowledged{0};
std::atomic<Listener> g_listener{nullptr};

void Append(Sev sev, int monitor, const wchar_t* source, const wchar_t* key, std::wstring title, std::wstring detail) {
    Event e;
    GetLocalTime(&e.time);
    e.sev = sev;
    e.monitor = monitor;
    e.source = source ? source : L"";
    if (key) e.key = key;
    e.title = std::move(title);
    e.detail = std::move(detail);
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        e.seq = ++g_seq;
        g_ring.push_back(std::move(e));
        while (g_ring.size() > kCapacity) g_ring.pop_front();
        g_lastSeq.store(g_seq, std::memory_order_release);
        g_revision.fetch_add(1, std::memory_order_acq_rel);
    }
    if (Listener fn = g_listener.load(std::memory_order_acquire)) fn();
}

}  // namespace

void Post(Sev sev, int monitor, const wchar_t* source, std::wstring title, std::wstring detail) {
    Append(sev, monitor, source, nullptr, std::move(title), std::move(detail));
}

void PostCondition(Sev sev, int monitor, const wchar_t* source, const wchar_t* key, std::wstring title,
                   std::wstring detail) {
    Append(sev, monitor, source, key && *key ? key : L"-", std::move(title), std::move(detail));
}

void Resolve(int monitor, const wchar_t* source, const wchar_t* key) {
    const wchar_t* src = source ? source : L"";
    const wchar_t* k = key && *key ? key : L"-";
    bool changed = false;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        for (Event& e : g_ring) {
            if (e.key.empty() || e.resolvedSeq != 0) continue;
            if (monitor != kAnyMonitor && e.monitor != monitor) continue;
            if (e.key != k || e.source != src) continue;
            e.resolvedSeq = g_seq;
            changed = true;
        }
        if (changed) g_revision.fetch_add(1, std::memory_order_acq_rel);
    }
    if (changed)
        if (Listener fn = g_listener.load(std::memory_order_acquire)) fn();
}

std::vector<Event> Snapshot(size_t max) {
    std::lock_guard<std::mutex> lock(g_mutex);
    size_t n = g_ring.size() < max ? g_ring.size() : max;
    return std::vector<Event>(g_ring.end() - (ptrdiff_t)n, g_ring.end());
}

uint64_t LastSeq() { return g_lastSeq.load(std::memory_order_acquire); }

uint64_t Revision() { return g_revision.load(std::memory_order_acquire); }

bool Acknowledge(uint64_t seq) {
    uint64_t cur = g_acknowledged.load(std::memory_order_acquire);
    while (seq > cur) {
        if (g_acknowledged.compare_exchange_weak(cur, seq, std::memory_order_acq_rel)) {
            g_revision.fetch_add(1, std::memory_order_acq_rel);
            return true;
        }
    }
    return false;
}

uint64_t Acknowledged() { return g_acknowledged.load(std::memory_order_acquire); }

bool Headline(Event& out) {
    std::lock_guard<std::mutex> lock(g_mutex);
    const Event* e = PickHeadline(g_ring, g_acknowledged.load(std::memory_order_acquire));
    if (!e) return false;
    out = *e;
    return true;
}

void SetListener(Listener fn) { g_listener.store(fn, std::memory_order_release); }

void ResetForTest() {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_ring.clear();
    g_seq = 0;
    g_lastSeq.store(0, std::memory_order_release);
    g_acknowledged.store(0, std::memory_order_release);
    g_revision.fetch_add(1, std::memory_order_acq_rel);
}

}  // namespace notify
