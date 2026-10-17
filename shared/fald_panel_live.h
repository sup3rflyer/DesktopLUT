// DesktopLUT - fald_panel_live.h
// FALD panel file live delivery (dwm_hook_config.h, "FALD panel file live delivery"): the delivery block's layout
// checks, the table of panel files the DWM hook holds per (monitor position, mode) with its lock and the layer's build
// decision, and the host's bookkeeping of what the hook answered. CPU only — no D3D, no hook internals — so the host
// (writes blocks, reads verdicts), the DLL (reads blocks, keeps the table) and the unit tests (tests/test_fald_live.cpp)
// run the same code: the protocol is tested where it runs.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string>
#include <vector>
#include <atomic>

#include "dwm_hook_config.h"
#include "fald_panel.h"

// Host: the size of a delivery block (header + payload) for a payload of `payloadBytes`.
size_t FaldPanelBlobBytes(size_t payloadBytes);
// Host: write the header and the payload (the panel file's bytes, unchanged) into `dst` (`dstBytes` >= FaldPanelBlobBytes).
// The acknowledgement words are written 0. False (nothing written) for an empty or oversized payload or a short `dst`.
bool FaldPanelBlobWrite(void* dst, size_t dstBytes, uint32_t gen, int left, int top, bool isHdr,
                        const void* payload, size_t payloadBytes);
// DLL: does the header `h` (copied out of a view of `viewBytes` bytes) describe delivery `gen` for (left, top, isHdr),
// with its whole payload inside the view? `why` (never null on false) names the first check that failed.
bool FaldPanelBlobCheck(const DwmHookFaldPanelBlob& h, size_t viewBytes, uint32_t gen, int left, int top, bool isHdr,
                        const char** why);
// DLL: may the verdict on a slot's file be written into the section the slot keeps? Only a delivered file (slotGen != 0)
// answers, and only in its own generation's section (a refused later delivery never replaced that section).
inline bool FaldPanelAckApplies(uint32_t slotGen, uint32_t viewGen) { return slotGen != 0 && viewGen == slotGen; }
// DLL: does an ingest keep the section it just answered in? Only an accepted one (its later verdicts go there).
inline bool FaldPanelKeepsView(uint32_t ack) { return ack == DWM_HOOK_FALD_PANEL_ACK_INGESTED; }

// One panel file the hook holds: the one staged at injection, or the last one delivered live.
struct FaldPanelSlot {
    int left = 0, top = 0;
    bool isHdr = false;
    bool present = false;            // `params` holds a parsed file (false: only refused deliveries so far)
    FaldPanelParams params;
    uint32_t seq = 0;                // content version: +1 for every change of the slot's file (staged, delivered,
                                     // reverted). The layer rebuilds when the seq it was built from differs.
    uint32_t gen = 0;                // delivery generation of the file in `params` (0 = the file staged at injection)
    uint32_t seenGen = 0;            // the last delivery generation processed, accepted or refused (never processed twice)
    uint32_t seenAck = DWM_HOOK_FALD_PANEL_ACK_NONE;   // its verdict
    // The file a delivered one replaced, kept until the layer has BUILT the new one (Commit): a file that cannot be
    // built is reverted to it (RevertBuild), so the slot never holds a file the layer cannot run while an older one
    // could. Set by the first delivery after the last build; later unbuilt deliveries replace only `params`.
    bool hasPrev = false;
    bool prevPresent = false;
    uint32_t prevGen = 0;
    FaldPanelParams prev;
    // This slot's own parse buffer (never shared between slots — two monitors may ingest on two present threads): the
    // memory of a file the slot no longer needs comes back here, so an A/B allocates nothing after the first switches.
    FaldPanelParams spare;
};

// The table's lock. DWM may run the Present hooks on more than one thread, and a delivery or a build mutates the table
// (slot files are heap-owning). On the present path it is only ever TRIED: a busy table skips this present's table work
// (taken again on the next present) — never a wait inside DWM's present. Blocking acquisition is for attach / release.
// An SRWLOCK underneath (pointer-sized; kept as a void* so this header needs no <windows.h>).
class FaldPanelLock {
public:
    bool TryLock();
    void Lock();
    void Unlock();
private:
    void* srw_ = nullptr;   // SRWLOCK_INIT
};
// RAII: tries on construction (or waits, `wait` = true), releases on destruction if owned.
class FaldPanelLockGuard {
public:
    explicit FaldPanelLockGuard(FaldPanelLock& l, bool wait = false) : l_(l) {
        if (wait) { l_.Lock(); owned_ = true; } else owned_ = l_.TryLock();
    }
    ~FaldPanelLockGuard() { if (owned_) l_.Unlock(); }
    bool owned() const { return owned_; }
    FaldPanelLockGuard(const FaldPanelLockGuard&) = delete;
    FaldPanelLockGuard& operator=(const FaldPanelLockGuard&) = delete;
private:
    FaldPanelLock& l_;
    bool owned_ = false;
};

// Fixed capacity, append-only, never reallocates (like the rest of the hook's per-monitor arrays): a pointer handed out
// stays valid until Clear(). One slot per (position, mode). NOT thread-safe by itself: the hook holds a FaldPanelLock
// around every call (and around every use of a slot's file).
class FaldPanelTable {
public:
    static const int kCapacity = 2 * MAX_DWM_HOOK_MONITORS;   // one SDR + one HDR per monitor

    void Clear();
    int Count() const { return n_; }
    const FaldPanelSlot& At(int i) const { return slots_[i]; }
    int IndexOf(int left, int top, bool isHdr) const;          // -1 = no slot
    const FaldPanelSlot* Find(int left, int top, bool isHdr) const;
    bool AnyPresent() const;

    // A file staged at injection, already parsed and checked against the mode by the caller. False when full.
    bool AddStaged(int left, int top, bool isHdr, FaldPanelParams&& params);
    // Has delivery `gen` for (left, top, isHdr) not been processed yet? (gen 0 = none published: false.) Also false
    // for a generation processed while the table was full: a generation is processed once, whatever happened.
    bool Wants(int left, int top, bool isHdr, uint32_t gen) const;
    // Process delivery `gen`: parse `payload` (ParseFaldPanelParams) and check its transfer against the mode. Accepted:
    // the slot's file is replaced (seq + 1) -> DWM_HOOK_FALD_PANEL_ACK_INGESTED. Refused (any failure, `err` says why;
    // a full table too): the slot keeps exactly what it held -> DWM_HOOK_FALD_PANEL_ACK_REFUSED. Either way `gen`
    // counts as processed (marked before the parse: even a fault inside it is never retried). `payload` may be
    // another process's memory: the parser reads its header words once and bounds every copy by `bytes`.
    uint32_t Deliver(int left, int top, bool isHdr, uint32_t gen, const void* payload, size_t bytes, std::string& err);
    // `gen` processed without content (its section could not be opened or its header was refused): nothing changes.
    void MarkSeen(int left, int top, bool isHdr, uint32_t gen, uint32_t ack);
    // The layer built the slot's current file: the file it replaced is no longer needed (its memory is reused).
    void Commit(int left, int top, bool isHdr);
    // The layer could NOT build the slot's current file. A delivered file goes back to the one it replaced (the last
    // built / staged file; none, if the slot had none): returns true and the slot's new seq in `seq`. False for a file
    // with nothing to go back to (the staged file, or one already reverted): nothing changes.
    bool RevertBuild(int left, int top, bool isHdr, uint32_t* seq);

private:
    FaldPanelSlot* FindOrAdd(int left, int top, bool isHdr);   // null when full
    FaldPanelSlot* FindMutable(int left, int top, bool isHdr);
    // A generation processed for a (position, mode) the full table had no slot for: remembered here so Wants() is
    // false for it (one record per (position, mode), its latest generation; oldest replaced when this is full too).
    struct Overflow { int left = 0, top = 0; bool isHdr = false; bool used = false; uint32_t gen = 0; };
    void RecordOverflow(int left, int top, bool isHdr, uint32_t gen);
    FaldPanelSlot slots_[kCapacity];
    int n_ = 0;
    Overflow overflow_[kCapacity];
    int overflowNext_ = 0;
};

// The gate between the presents that USE a layer entry lock-free and the lock holder that REBUILDS it (dwm_hook/
// hook_fald.cpp: a live swap, a build, a stale mark, a release). Neither side ever waits:
//   - a present that wants to use the entry BORROWS it (TryBorrow) for the rest of its present and RETURNS it at the
//     end (Return, on every path); a borrow that meets a rebuild backs off — the present runs without the layer;
//   - a rebuilder (holding the table lock) claims the entry (TryBeginRebuild) before it touches the entry's resources
//     and releases the claim after (EndRebuild); a claim that meets a borrow — or another claim — is refused, and the
//     work stays pending for a later present (the caller asks for one).
// Both sides announce first and look second, sequentially consistent (Dekker): of a borrow and a claim that overlap,
// at least one sees the other, so a rebuild never runs under a borrower and a borrower never uses a half-rebuilt entry.
// A borrower re-checks, after borrowing, that the entry it found is still the one in the list (a release between its
// lookup and its borrow), because a release, too, claims the gate first.
struct FaldEntryGate {
    std::atomic<int> borrows{ 0 };
    std::atomic<bool> rebuilding{ false };

    bool TryBorrow() {
        borrows.fetch_add(1);
        if (rebuilding.load()) { borrows.fetch_sub(1); return false; }
        return true;
    }
    void Return() { borrows.fetch_sub(1); }
    bool TryBeginRebuild() {
        bool expected = false;
        if (!rebuilding.compare_exchange_strong(expected, true)) return false;   // another claim (a release on its
        if (borrows.load() != 0) { rebuilding.store(false); return false; }      // timeout path, or a lock holder)
        return true;
    }
    void EndRebuild() { rebuilding.store(false); }
    // A new entry, not yet visible to anyone: no borrows, claimed by its builder.
    void ClaimNew() { borrows.store(0); rebuilding.store(true); }
    bool Borrowed() const { return borrows.load() != 0; }
};

// The layer's build decision for one monitor entry in one mode (dwm_hook/hook_fald.cpp FaldAcquire), pure so the latch
// rules are tested: what the entry was built from / failed on, against the slot's current file.
struct FaldLayerBuildState {
    bool valid = false;          // the entry runs (resources built)
    bool failed = false;         // a build failed: latched for that file
    uint32_t panelSeq = 0;       // the slot seq it was built from
    uint32_t failedSeq = 0;      // the slot seq the latched failure was for
    uint32_t refusedSeq = 0;     // a live swap to this slot seq failed (the entry runs on, on its previous file)
};
enum class FaldLayerAction {
    Run,      // use the entry as it is
    Swap,     // a running entry, a newer file: swap the panel part between two presents (keep the old on failure)
    Build,    // build the entry from the slot's file (first sight, resize, released, or a newer file after a failure)
    Off,      // no file, or a latched failure on this very file
};
// `sameFrame` = the entry's size and format match this present's back buffer; `slot` = the slot's file (null or
// !present = none).
FaldLayerAction FaldLayerDecide(const FaldLayerBuildState& e, bool sameFrame, const FaldPanelSlot* slot);

// ---------------------------------------------------------------------------------------------------------------------
// Host side: what the hook answered about the last delivery for one (position, mode), and what it therefore HOLDS.
// The host names a delivered file in its staging set at once (the live-layer reconcile must not re-inject for the path
// change), so it has to take that back the moment the hook says otherwise: FaldPanelTrackStep is that rule, pure.
// ---------------------------------------------------------------------------------------------------------------------
// Host-only verdict past DWM_HOOK_FALD_PANEL_ACK_*: no answer within the time a present of the display would give.
constexpr uint32_t FALD_PANEL_VERDICT_NO_ACK = 0x100u;

struct FaldPanelTrack {
    std::wstring heldPath;     // the file the hook holds for this (position, mode) as far as its verdicts say
    uint32_t heldGen = 0;      // its generation (0 = staged at injection)
    std::wstring basePath;     // the last file the hook staged or BUILT: what a failed build reverts to (RevertBuild)
    uint32_t baseGen = 0;
    std::wstring sentPath;     // the last file delivered (gen != 0)
    uint32_t gen = 0;          // its generation; 0 = nothing delivered since the injection
    uint32_t verdict = DWM_HOOK_FALD_PANEL_ACK_NONE;   // the hook's answer on `gen` (or FALD_PANEL_VERDICT_NO_ACK)
    unsigned waitTicks = 0;    // watchdog ticks counted without an answer
    bool final = true;         // `gen` is decided (applied / refused / build failed / no answer): nothing more to read

    // Injection staged `path` (or nothing): every delivery of the old DLL is over.
    void Stage(const std::wstring& path);
    // A delivery of `path` as `generation` was published (the section is complete).
    void Sent(const std::wstring& path, uint32_t generation);
};

enum class FaldPanelStep {
    None,          // nothing new
    Ingested,      // the hook holds the file (the layer takes it at its next run in that mode)
    Applied,       // the layer runs on it
    Failed,        // refused / could not be built / no answer: the hook holds `heldPath`, not the file sent
};
// Fold one reading of the section's acknowledgement words into `t`. `countTick` = a watchdog tick on which a present of
// the display was due (display on, attached): it counts toward `maxTicks` without an answer -> NO_ACK. An answer for
// another generation is ignored. A step is returned once; after a final one the track ignores further readings.
FaldPanelStep FaldPanelTrackStep(FaldPanelTrack& t, uint32_t ackGen, uint32_t ackStatus, bool countTick,
                                 unsigned maxTicks);
// Should the host ask DWM for a full composition for this delivery now? Only on a watchdog tick, while a present of the
// display is due (display on, not occluded, `attached` to the desktop) and the hook has given NO answer at all on it:
// a held-but-idle file (ingested: the display is in the other mode, its layer is off, its routing is not trusted) has
// answered — a full-screen topmost window every tick for it would drop G-Sync and spoil pacing, for nothing.
bool FaldPanelTrackWantsRecompose(const FaldPanelTrack& t, bool watchdogTick, bool presentsDue, bool attached);
// The generation the shared config carries for this (position, mode): the delivery's, until it went unanswered
// (no_ack) — then 0, so a late ingest cannot leave the hook holding a file the host says it does not.
uint32_t FaldPanelTrackPublishedGen(const FaldPanelTrack& t);

// The host falls back to a re-injection once per failed file STAMP (position, mode, path, size, mtime): a file that
// fails again after that re-injection must not re-inject forever. Kept across injections.
class FaldPanelFallbackGuard {
public:
    // True the first time this (position, mode, file stamp) asks; false for every repeat.
    bool Allow(int left, int top, bool isHdr, const std::wstring& path, unsigned long long size,
               unsigned long long mtime);
    size_t Count() const { return keys_.size(); }
private:
    struct Key { int left, top; bool isHdr; std::wstring path; unsigned long long size, mtime; };
    std::vector<Key> keys_;
};

// 64-bit FNV-1a of a file image: the host skips re-delivering a re-export whose bytes did not change (a touch-only
// mtime change would otherwise reset the layer's LED-lag state for nothing).
uint64_t FaldPanelContentHash(const void* data, size_t bytes);
