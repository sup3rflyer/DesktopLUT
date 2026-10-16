// DesktopLUT - fald_panel_live.h
// FALD panel file live delivery (dwm_hook_config.h, "FALD panel file live delivery"): the delivery block's layout
// checks and the table of panel files the DWM hook holds per (monitor position, mode). Pure CPU — no D3D, no Win32
// object, no hook internals — so the host (writes blocks), the DLL (reads them, keeps the table) and the unit tests
// (tests/test_fald_live.cpp) run the same code: the protocol is tested where it runs.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string>

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

// One panel file the hook holds: the one staged at injection, or the last one delivered live.
struct FaldPanelSlot {
    int left = 0, top = 0;
    bool isHdr = false;
    bool present = false;            // `params` holds a parsed file (false: only refused deliveries so far)
    FaldPanelParams params;
    uint32_t seq = 0;                // content version: +1 for every file accepted into this slot (staged or delivered).
                                     // The layer rebuilds when the seq it was built from differs.
    uint32_t gen = 0;                // delivery generation of the file in `params` (0 = the file staged at injection)
    uint32_t seenGen = 0;            // the last delivery generation processed, accepted or refused (never processed twice)
    uint32_t seenAck = DWM_HOOK_FALD_PANEL_ACK_NONE;   // its verdict
};

// Fixed capacity, append-only, never reallocates (like the rest of the hook's per-monitor arrays): a pointer handed out
// stays valid until Clear(). One slot per (position, mode); the hook's present path is its only writer.
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
    // Has delivery `gen` for (left, top, isHdr) not been processed yet? (gen 0 = none published: false.)
    bool Wants(int left, int top, bool isHdr, uint32_t gen) const;
    // Process delivery `gen`: parse `payload` (ParseFaldPanelParams) and check its transfer against the mode. Accepted:
    // the slot's file is replaced (seq + 1) -> DWM_HOOK_FALD_PANEL_ACK_INGESTED. Refused (any failure, `err` says why):
    // the slot keeps exactly what it held -> DWM_HOOK_FALD_PANEL_ACK_REFUSED. Either way `gen` counts as processed
    // (marked before the parse: even a fault inside it is never retried). `payload` may be another process's memory:
    // the parser reads its header words once and bounds every copy by `bytes`.
    uint32_t Deliver(int left, int top, bool isHdr, uint32_t gen, const void* payload, size_t bytes, std::string& err);
    // `gen` processed without content (its section could not be opened or its header was refused): nothing changes.
    void MarkSeen(int left, int top, bool isHdr, uint32_t gen, uint32_t ack);

private:
    FaldPanelSlot* FindOrAdd(int left, int top, bool isHdr);   // null when full
    FaldPanelSlot slots_[kCapacity];
    int n_ = 0;
    FaldPanelParams spare_;   // Deliver parses here; an accepted file is swapped into its slot (memory reused)
};
