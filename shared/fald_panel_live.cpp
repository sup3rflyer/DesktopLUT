// DesktopLUT - fald_panel_live.cpp
// FALD panel file live delivery: block checks, the hook's panel table, the host's verdict bookkeeping.
// See fald_panel_live.h.
#include "fald_panel_live.h"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <cstring>
#include <utility>

size_t FaldPanelBlobBytes(size_t payloadBytes) { return sizeof(DwmHookFaldPanelBlob) + payloadBytes; }

bool FaldPanelBlobWrite(void* dst, size_t dstBytes, uint32_t gen, int left, int top, bool isHdr,
                        const void* payload, size_t payloadBytes) {
    if (!dst || !payload || payloadBytes == 0 || payloadBytes > DWM_HOOK_FALD_PANEL_MAX_BYTES ||
        dstBytes < FaldPanelBlobBytes(payloadBytes))
        return false;
    DwmHookFaldPanelBlob h;
    std::memset(&h, 0, sizeof(h));
    h.magic = DWM_HOOK_FALD_PANEL_MAGIC;
    h.headerBytes = (uint32_t)sizeof(DwmHookFaldPanelBlob);
    h.gen = gen;
    h.left = left;
    h.top = top;
    h.isHdr = isHdr ? 1u : 0u;
    h.payloadBytes = (uint32_t)payloadBytes;
    std::memcpy(dst, &h, sizeof(h));
    std::memcpy(static_cast<unsigned char*>(dst) + sizeof(h), payload, payloadBytes);
    return true;
}

bool FaldPanelBlobCheck(const DwmHookFaldPanelBlob& h, size_t viewBytes, uint32_t gen, int left, int top, bool isHdr,
                        const char** why) {
    const char* dummy = nullptr;
    const char** w = why ? why : &dummy;
    if (h.magic != DWM_HOOK_FALD_PANEL_MAGIC) { *w = "bad magic"; return false; }
    if (h.headerBytes != sizeof(DwmHookFaldPanelBlob)) { *w = "header size"; return false; }
    if (gen == 0 || h.gen != gen) { *w = "generation mismatch"; return false; }
    if (h.left != left || h.top != top) { *w = "monitor position mismatch"; return false; }
    if (h.isHdr != (isHdr ? 1u : 0u)) { *w = "mode mismatch"; return false; }
    if (h.payloadBytes == 0 || h.payloadBytes > DWM_HOOK_FALD_PANEL_MAX_BYTES) { *w = "payload size"; return false; }
    if (viewBytes < sizeof(DwmHookFaldPanelBlob) || h.payloadBytes > viewBytes - sizeof(DwmHookFaldPanelBlob)) {
        *w = "payload extends past the section";
        return false;
    }
    return true;
}

void FaldPanelTable::Clear() {
    for (int i = 0; i < n_; i++) slots_[i] = FaldPanelSlot{};
    n_ = 0;
    for (int i = 0; i < kCapacity; i++) overflow_[i] = Overflow{};
    overflowNext_ = 0;
}

int FaldPanelTable::IndexOf(int left, int top, bool isHdr) const {
    for (int i = 0; i < n_; i++)
        if (slots_[i].left == left && slots_[i].top == top && slots_[i].isHdr == isHdr) return i;
    return -1;
}

const FaldPanelSlot* FaldPanelTable::Find(int left, int top, bool isHdr) const {
    const int i = IndexOf(left, top, isHdr);
    return i >= 0 ? &slots_[i] : nullptr;
}

FaldPanelSlot* FaldPanelTable::FindMutable(int left, int top, bool isHdr) {
    const int i = IndexOf(left, top, isHdr);
    return i >= 0 ? &slots_[i] : nullptr;
}

bool FaldPanelTable::AnyPresent() const {
    for (int i = 0; i < n_; i++)
        if (slots_[i].present) return true;
    return false;
}

FaldPanelSlot* FaldPanelTable::FindOrAdd(int left, int top, bool isHdr) {
    const int i = IndexOf(left, top, isHdr);
    if (i >= 0) return &slots_[i];
    if (n_ >= kCapacity) return nullptr;
    FaldPanelSlot& s = slots_[n_];
    s = FaldPanelSlot{};
    s.left = left; s.top = top; s.isHdr = isHdr;
    n_++;   // published last: a reader either does not see the slot or sees it complete
    return &s;
}

void FaldPanelTable::RecordOverflow(int left, int top, bool isHdr, uint32_t gen) {
    Overflow* unused = nullptr;
    for (int i = 0; i < kCapacity; i++) {
        Overflow& o = overflow_[i];
        if (o.used && o.left == left && o.top == top && o.isHdr == isHdr) { o.gen = gen; return; }
        if (!o.used && !unused) unused = &o;
    }
    // The shared config names at most 2 * MAX_DWM_HOOK_MONITORS (position, mode) pairs at a time, so with one record
    // each this never wraps in practice; if it does, the oldest record goes (that pair is then processed once more).
    Overflow& o = unused ? *unused : overflow_[(overflowNext_++) % kCapacity];
    o.used = true; o.left = left; o.top = top; o.isHdr = isHdr; o.gen = gen;
}

bool FaldPanelTable::AddStaged(int left, int top, bool isHdr, FaldPanelParams&& params) {
    FaldPanelSlot* s = FindOrAdd(left, top, isHdr);
    if (!s) return false;
    s->params = std::move(params);
    s->present = true;
    s->gen = 0;
    s->seq++;
    s->hasPrev = false;
    return true;
}

bool FaldPanelTable::Wants(int left, int top, bool isHdr, uint32_t gen) const {
    if (gen == 0) return false;
    if (const FaldPanelSlot* s = Find(left, top, isHdr)) return s->seenGen != gen;
    for (int i = 0; i < kCapacity; i++) {
        const Overflow& o = overflow_[i];
        if (o.used && o.left == left && o.top == top && o.isHdr == isHdr && o.gen == gen) return false;
    }
    return true;
}

void FaldPanelTable::MarkSeen(int left, int top, bool isHdr, uint32_t gen, uint32_t ack) {
    FaldPanelSlot* s = FindOrAdd(left, top, isHdr);
    if (!s) { RecordOverflow(left, top, isHdr, gen); return; }
    s->seenGen = gen;
    s->seenAck = ack;
}

uint32_t FaldPanelTable::Deliver(int left, int top, bool isHdr, uint32_t gen, const void* payload, size_t bytes,
                                 std::string& err) {
    FaldPanelSlot* s = FindOrAdd(left, top, isHdr);
    if (!s) {   // processed all the same: never opened, mapped and refused again on every present
        RecordOverflow(left, top, isHdr, gen);
        err = "no free panel slot";
        return DWM_HOOK_FALD_PANEL_ACK_REFUSED;
    }
    s->seenGen = gen;
    s->seenAck = DWM_HOOK_FALD_PANEL_ACK_REFUSED;   // until the file is in (a fault inside the parse leaves this)
    // Parsed into the slot's own spare, moved in only when accepted: a refusal leaves the slot's file exactly as it was.
    // The memory of a file the slot no longer needs comes back as its spare (Commit, RevertBuild, an unbuilt file replaced),
    // so an A/B of two files allocates nothing in DWM's present after the first switches.
    if (!ParseFaldPanelParams(payload, bytes, s->spare, err)) return s->seenAck;
    if (!FaldTransferMatchesMode(s->spare.transfer, isHdr)) {
        err = isHdr ? "a gamma (SDR) fit delivered for HDR" : "a PQ (HDR) fit delivered for SDR (ACM)";
        return s->seenAck;
    }
    if (!s->hasPrev) {   // the first delivery since the layer last built this slot's file: keep that one to revert to
        s->prev = std::move(s->params);
        s->prevPresent = s->present;
        s->prevGen = s->gen;
        s->hasPrev = true;
        s->params = std::move(s->spare);
        s->spare = FaldPanelParams{};
    } else {             // an earlier delivery the layer never built: replaced (its memory becomes the spare)
        std::swap(s->params, s->spare);
    }
    s->present = true;
    s->gen = gen;
    s->seq++;
    s->seenAck = DWM_HOOK_FALD_PANEL_ACK_INGESTED;
    return s->seenAck;
}

void FaldPanelTable::Commit(int left, int top, bool isHdr) {
    FaldPanelSlot* s = FindMutable(left, top, isHdr);
    if (!s || !s->hasPrev) return;
    s->spare = std::move(s->prev);
    s->prev = FaldPanelParams{};
    s->hasPrev = false;
    s->prevPresent = false;
    s->prevGen = 0;
}

bool FaldPanelTable::RevertBuild(int left, int top, bool isHdr, uint32_t* seq) {
    FaldPanelSlot* s = FindMutable(left, top, isHdr);
    if (!s || !s->hasPrev) return false;
    std::swap(s->params, s->prev);
    s->spare = std::move(s->prev);   // the file that could not be built
    s->prev = FaldPanelParams{};
    s->present = s->prevPresent;
    s->gen = s->prevGen;
    s->hasPrev = false;
    s->prevPresent = false;
    s->prevGen = 0;
    s->seq++;
    if (seq) *seq = s->seq;
    return true;
}

// ---------------------------------------------------------------------------------------------------------------------
// Host bookkeeping
// ---------------------------------------------------------------------------------------------------------------------
void FaldPanelTrack::Stage(const std::wstring& path) {
    heldPath = basePath = path;
    heldGen = baseGen = 0;
    sentPath.clear();
    gen = 0;
    verdict = DWM_HOOK_FALD_PANEL_ACK_NONE;
    waitTicks = 0;
    final = true;
}

void FaldPanelTrack::Sent(const std::wstring& path, uint32_t generation) {
    sentPath = path;
    gen = generation;
    verdict = DWM_HOOK_FALD_PANEL_ACK_NONE;
    waitTicks = 0;
    final = false;
}

FaldPanelStep FaldPanelTrackStep(FaldPanelTrack& t, uint32_t ackGen, uint32_t ackStatus, bool countTick,
                                 unsigned maxTicks) {
    if (t.final || t.gen == 0) return FaldPanelStep::None;
    if (ackGen != t.gen || ackStatus == DWM_HOOK_FALD_PANEL_ACK_NONE) {   // no answer on this generation yet
        if (!countTick) return FaldPanelStep::None;
        if (++t.waitTicks < maxTicks) return FaldPanelStep::None;
        t.verdict = FALD_PANEL_VERDICT_NO_ACK;   // nothing taken, as far as anyone can tell: the hook keeps its file
        t.final = true;
        return FaldPanelStep::Failed;
    }
    if (ackStatus == t.verdict) return FaldPanelStep::None;
    switch (ackStatus) {
    case DWM_HOOK_FALD_PANEL_ACK_INGESTED:     // held; the build verdict follows when the layer runs in that mode
        t.verdict = ackStatus;
        t.heldPath = t.sentPath; t.heldGen = t.gen;
        return FaldPanelStep::Ingested;
    case DWM_HOOK_FALD_PANEL_ACK_APPLIED:      // built and running: also what a later failed build reverts to
        t.verdict = ackStatus;
        t.heldPath = t.basePath = t.sentPath; t.heldGen = t.baseGen = t.gen;
        t.final = true;
        return FaldPanelStep::Applied;
    case DWM_HOOK_FALD_PANEL_ACK_REFUSED:      // the slot never changed
        t.verdict = ackStatus;
        t.final = true;
        return FaldPanelStep::Failed;
    case DWM_HOOK_FALD_PANEL_ACK_BUILD_FAILED: // the DLL went back to its last built / staged file (RevertBuild)
        t.verdict = ackStatus;
        t.heldPath = t.basePath; t.heldGen = t.baseGen;
        t.final = true;
        return FaldPanelStep::Failed;
    default:                                    // a word this host does not know: no answer
        return FaldPanelStep::None;
    }
}

bool FaldPanelTrackWantsRecompose(const FaldPanelTrack& t, bool watchdogTick, bool presentsDue, bool attached) {
    return watchdogTick && presentsDue && attached && !t.final && t.gen != 0 && t.verdict == DWM_HOOK_FALD_PANEL_ACK_NONE;
}

uint32_t FaldPanelTrackPublishedGen(const FaldPanelTrack& t) {
    return t.verdict == FALD_PANEL_VERDICT_NO_ACK ? 0u : t.gen;
}

bool FaldPanelFallbackGuard::Allow(int left, int top, bool isHdr, const std::wstring& path, unsigned long long size,
                                   unsigned long long mtime) {
    for (const Key& k : keys_)
        if (k.left == left && k.top == top && k.isHdr == isHdr && k.size == size && k.mtime == mtime && k.path == path)
            return false;
    keys_.push_back(Key{ left, top, isHdr, path, size, mtime });
    return true;
}

// ---------------------------------------------------------------------------------------------------------------------
// Lock, build decision, content hash
// ---------------------------------------------------------------------------------------------------------------------
static_assert(sizeof(SRWLOCK) == sizeof(void*), "FaldPanelLock keeps the SRWLOCK in a void*");

bool FaldPanelLock::TryLock() { return TryAcquireSRWLockExclusive(reinterpret_cast<PSRWLOCK>(&srw_)) != FALSE; }
void FaldPanelLock::Lock() { AcquireSRWLockExclusive(reinterpret_cast<PSRWLOCK>(&srw_)); }
void FaldPanelLock::Unlock() { ReleaseSRWLockExclusive(reinterpret_cast<PSRWLOCK>(&srw_)); }

FaldLayerAction FaldLayerDecide(const FaldLayerBuildState& e, bool sameFrame, const FaldPanelSlot* slot) {
    const bool file = slot && slot->present;
    if (sameFrame && e.valid) {
        if (!file || slot->seq == e.panelSeq || slot->seq == e.refusedSeq) return FaldLayerAction::Run;
        return FaldLayerAction::Swap;
    }
    if (sameFrame && e.failed && (!file || slot->seq == e.failedSeq)) return FaldLayerAction::Off;
    return file ? FaldLayerAction::Build : FaldLayerAction::Off;
}

uint64_t FaldPanelContentHash(const void* data, size_t bytes) {
    uint64_t h = 1469598103934665603ull;
    const unsigned char* p = static_cast<const unsigned char*>(data);
    for (size_t i = 0; i < bytes; i++) { h ^= p[i]; h *= 1099511628211ull; }
    return h;
}
