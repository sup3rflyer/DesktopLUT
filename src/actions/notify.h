#pragma once
// Notification log: one place every user-facing status, success, warning and error is recorded.
//
// Post() is safe from any thread (render, processing, pipe, whitelist, GUI): it only takes a mutex
// and appends to a bounded ring, then calls the registered listener (which must itself be
// thread-safe; the UI's is a coalesced PostMessage). Readers take a copy with Snapshot().
//
// SetStatus() (gui.h) records here too, so the interface's footer and its Events
// page read the same history.
//
// The footer's line (the HEADLINE, PickHeadline below): a warning stands there until it is over —
//   - a CONDITION warning (PostCondition: "rate-capped", "could not be applied, retrying") until the code that knows
//     the condition ended says so with Resolve(monitor, source, key);
//   - any warning until the user has SEEN it: opening the Events page acknowledges everything up to the newest
//     event (Acknowledge). No timer: an old warning nobody looked at still says when it happened.
// Nothing is ever removed from the ring for this: resolving or acknowledging only changes the footer's pick.

#include <windows.h>
#include <climits>
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
    std::wstring key;            // non-empty: a CONDITION (PostCondition) that Resolve(monitor, source, key) ends
    uint64_t     resolvedSeq = 0;// the condition ended: the newest seq when Resolve() ran (0 = standing / one-off)
};

constexpr size_t kCapacity = 500;
constexpr int kAnyMonitor = INT_MIN;   // Resolve(): every monitor's condition of that source + key

void Post(Sev sev, int monitor, const wchar_t* source, std::wstring title, std::wstring detail = {});

// A warning or error that describes a CONDITION: it stands (in the footer) until Resolve(monitor, source, key) —
// or until the user has seen it, like any warning. `key` names the condition when one source has several.
void PostCondition(Sev sev, int monitor, const wchar_t* source, const wchar_t* key, std::wstring title,
                   std::wstring detail = {});

// The condition ended: every standing PostCondition event of this source + key on `monitor` (kAnyMonitor = all) is
// marked resolved. Posts nothing (post the end's own Info line separately when it is news). Safe from any thread;
// cheap when nothing matches (one pass over the ring under the mutex). Calls the listener when something changed.
void Resolve(int monitor, const wchar_t* source, const wchar_t* key);

// Copy of the newest `max` events, oldest first.
std::vector<Event> Snapshot(size_t max = kCapacity);

// Sequence number of the newest event (0 = none yet). Cheap; lets a reader skip unchanged frames.
uint64_t LastSeq();

// Changes on every Post, effective Resolve and Acknowledge: what a reader of the headline / resolved marks keys on.
uint64_t Revision();

// The user has seen every event up to `seq` (the Events page is open). Never moves back. Per process, not saved.
// True when it moved (the headline may have changed).
bool Acknowledge(uint64_t seq);
uint64_t Acknowledged();

inline bool IsWarning(Sev s) { return s == Sev::Warning || s == Sev::Error; }

// The footer's pick, pure. `events` oldest first (a Snapshot or the ring itself), walked whole, newest first:
//   1. the newest warning / error that is not resolved and newer than `acknowledged`;
//   2. else the newest "status" line;
//   3. else none (nullptr).
// The walk is bounded only by what the ring still holds (kCapacity): an event the ring dropped is gone from Events
// too, so the footer never names what Events cannot show.
template <class Seq>
const Event* PickHeadline(const Seq& events, uint64_t acknowledged) {
    const Event* status = nullptr;
    for (auto it = events.rbegin(); it != events.rend(); ++it) {
        const Event& e = *it;
        if (IsWarning(e.sev)) {
            if (e.resolvedSeq == 0 && e.seq > acknowledged) return &e;
        } else if (!status && e.source == L"status") {
            status = &e;
        }
    }
    return status;
}

// PickHeadline over the live ring with the live acknowledgement: a copy of the pick, false = none.
bool Headline(Event& out);

// Called after every Post() (and every Resolve() that changed something), on the posting thread. Must be thread-safe
// and must not block.
using Listener = void (*)();
void SetListener(Listener fn);

// Test hook: drop every event and reset the sequence and the acknowledgement.
void ResetForTest();

}  // namespace notify
