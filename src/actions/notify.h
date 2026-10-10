#pragma once
// Notification log: one place every user-facing status, success, warning and error is recorded.
//
// Post() is safe from any thread (render, processing, pipe, whitelist, GUI): it only takes a mutex
// and appends to a bounded ring, then calls the registered listener (which must itself be
// thread-safe; the UI's is a coalesced PostMessage). Readers take a copy with Snapshot().
//
// SetStatus() (gui.h) records here too, so the classic one-line status bar and the new UI's Events
// page read the same history.

#include <windows.h>
#include <cstdint>
#include <string>
#include <vector>

namespace notify {

enum class Sev : uint8_t { Info, Success, Warning, Error };

struct Event {
    uint64_t     seq = 0;        // monotonically increasing, 1-based
    SYSTEMTIME   time = {};      // local time of Post()
    Sev          sev = Sev::Info;
    int          monitor = -1;   // live monitor index at post time, -1 = global
    std::wstring source;         // short tag: "status", "mhc", "hook", "lut", "fald", "hotkey", ...
    std::wstring title;
    std::wstring detail;
};

constexpr size_t kCapacity = 500;

void Post(Sev sev, int monitor, const wchar_t* source, std::wstring title, std::wstring detail = {});

// Copy of the newest `max` events, oldest first.
std::vector<Event> Snapshot(size_t max = kCapacity);

// Sequence number of the newest event (0 = none yet). Cheap; lets a reader skip unchanged frames.
uint64_t LastSeq();

// Called after every Post(), on the posting thread. Must be thread-safe and must not block.
using Listener = void (*)();
void SetListener(Listener fn);

// Test hook: drop every event and reset the sequence.
void ResetForTest();

}  // namespace notify
