// DesktopLUT - fald_panel_live.cpp
// FALD panel file live delivery: block checks + the hook's panel table. See fald_panel_live.h.
#include "fald_panel_live.h"

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
    spare_ = FaldPanelParams{};   // its memory too
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

bool FaldPanelTable::AddStaged(int left, int top, bool isHdr, FaldPanelParams&& params) {
    FaldPanelSlot* s = FindOrAdd(left, top, isHdr);
    if (!s) return false;
    s->params = std::move(params);
    s->present = true;
    s->gen = 0;
    s->seq++;
    return true;
}

bool FaldPanelTable::Wants(int left, int top, bool isHdr, uint32_t gen) const {
    if (gen == 0) return false;
    const FaldPanelSlot* s = Find(left, top, isHdr);
    return !s || s->seenGen != gen;
}

void FaldPanelTable::MarkSeen(int left, int top, bool isHdr, uint32_t gen, uint32_t ack) {
    FaldPanelSlot* s = FindOrAdd(left, top, isHdr);
    if (!s) return;
    s->seenGen = gen;
    s->seenAck = ack;
}

uint32_t FaldPanelTable::Deliver(int left, int top, bool isHdr, uint32_t gen, const void* payload, size_t bytes,
                                 std::string& err) {
    FaldPanelSlot* s = FindOrAdd(left, top, isHdr);
    if (!s) { err = "no free panel slot"; return DWM_HOOK_FALD_PANEL_ACK_REFUSED; }
    s->seenGen = gen;
    s->seenAck = DWM_HOOK_FALD_PANEL_ACK_REFUSED;   // until the file is in (a fault inside the parse leaves this)
    // Parsed into the spare, swapped in only when accepted: a refusal leaves the slot exactly as it was. The slot's
    // previous file becomes the next spare, so an A/B of two files reuses the same memory (no page faults in DWM's
    // present after the first two switches).
    if (!ParseFaldPanelParams(payload, bytes, spare_, err)) return s->seenAck;
    if (!FaldTransferMatchesMode(spare_.transfer, isHdr)) {
        err = isHdr ? "a gamma (SDR) fit delivered for HDR" : "a PQ (HDR) fit delivered for SDR (ACM)";
        return s->seenAck;
    }
    std::swap(s->params, spare_);
    s->present = true;
    s->gen = gen;
    s->seq++;
    s->seenAck = DWM_HOOK_FALD_PANEL_ACK_INGESTED;
    return s->seenAck;
}
