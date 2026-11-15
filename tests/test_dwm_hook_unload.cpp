#include "doctest.h"
#include "../shared/dwm_hook_unload.h"

#include <atomic>
#include <string>
#include <vector>

// ============================================================================
// Taking DwmHook.dll out of dwm.exe (shared/dwm_hook_unload.h): the DLL is unmapped only after it has seen that no
// thread is inside it. 2026-11-15: dwm.exe crashed in "DwmHook.dll_unloaded" (RenderLUT+0x1043) when a re-injection
// unloaded the DLL under a render thread that was still in a detour.
// Everything here runs in this process on its own threads and pages: no dwm.exe, no injection.
// ============================================================================

namespace {

// The drain's world, scripted: the count per poll, what the frozen look says, and the clock (advanced by Pause only).
struct FakeDrain {
    std::vector<long> insideSeq;          // Inside() per call; the last value repeats
    std::vector<int> outsideSeq;          // ThreadsOutside() per call; the last value repeats
    size_t insideCalls = 0, outsideCalls = 0;
    unsigned long long now = 1000;
    std::vector<unsigned> pauses;
    std::vector<long> insideAtLook;       // the count the drain had just read when it took a frozen look

    long last = 0;
    long Inside() {
        last = insideSeq[insideCalls < insideSeq.size() ? insideCalls : insideSeq.size() - 1];
        insideCalls++;
        return last;
    }
    int ThreadsOutside() {
        insideAtLook.push_back(last);
        const int r = outsideSeq[outsideCalls < outsideSeq.size() ? outsideCalls : outsideSeq.size() - 1];
        outsideCalls++;
        return r;
    }
    unsigned long long NowMs() { return now; }
    void Pause(unsigned ms) { pauses.push_back(ms); now += ms; }
};

// A thread that runs in a page of its own until it is told to stop: a stand-in for a thread of dwm.exe whose
// instruction pointer is inside the hook's code.
//   mov qword ptr [rcx+8], 1 ; entered
//   L: pause ; mov rax,[rcx] ; test rax,rax ; jz L
//   xor eax,eax ; ret
const unsigned char kSpinStub[] = {
    0x48, 0xC7, 0x41, 0x08, 0x01, 0x00, 0x00, 0x00,
    0xF3, 0x90, 0x48, 0x8B, 0x01, 0x48, 0x85, 0xC0, 0x74, 0xF6,
    0x31, 0xC0, 0xC3,
};
struct SpinFlags {
    volatile LONG64 stop = 0;
    volatile LONG64 entered = 0;
};

struct SpinThread {
    void* page = nullptr;
    SpinFlags flags;
    HANDLE thread = nullptr;
    bool Start() {
        page = VirtualAlloc(nullptr, 4096, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
        if (!page) return false;
        memcpy(page, kSpinStub, sizeof(kSpinStub));
        FlushInstructionCache(GetCurrentProcess(), page, sizeof(kSpinStub));
        thread = CreateThread(nullptr, 0, reinterpret_cast<LPTHREAD_START_ROUTINE>(page), &flags, 0, nullptr);
        if (!thread) return false;
        // No deadline: the thread sets the flag as its first instruction, and it will run.
        while (InterlockedCompareExchange64(&flags.entered, 1, 1) != 1) SwitchToThread();
        return true;
    }
    void Stop() {
        if (thread) {
            InterlockedExchange64(&flags.stop, 1);
            WaitForSingleObject(thread, INFINITE);
            CloseHandle(thread);
            thread = nullptr;
        }
    }
    DwmHookCodeRange Range() const {
        return { reinterpret_cast<uintptr_t>(page), reinterpret_cast<uintptr_t>(page) + 4096 };
    }
    ~SpinThread() {
        Stop();
        if (page) VirtualFree(page, 0, MEM_RELEASE);
    }
};

// The look, repeated while it could not be taken for every thread: another thread of this test exe may be ending
// just now (it can no longer be suspended and has not ended yet). The DLL's drain repeats it the same way.
// Bounded: a thread that can never be read fails the test (-1, with the reason in `look`) instead of hanging it.
int LookSettled(const DwmHookCodeRange* ranges, int numRanges, const DwmHookCallGate* gate, DwmHookLook* look = nullptr) {
    DwmHookLook local;
    DwmHookLook& info = look ? *look : local;
    for (int attempt = 0; attempt < 2000; attempt++) {
        const int r = DwmHookThreadsOutside(ranges, numRanges, gate, &info);
        if (r != -1) return r;
        Sleep(1);
    }
    MESSAGE("the look stayed incomplete: reason " << (int)info.unseen << ", thread " << info.unseenTid << ", error "
                                                  << info.unseenError);
    return -1;
}

}  // namespace

TEST_CASE("hook unload: a scope counts its thread in and out") {
    DwmHookCallGate gate;
    CHECK(gate.inside.load() == 0);
    {
        DwmHookCallScope a(gate);
        CHECK(gate.inside.load() == 1);
        {
            DwmHookCallScope b(gate);   // a second detour on the same thread (Present calls into a hooked predicate)
            CHECK(gate.inside.load() == 2);
        }
        CHECK(gate.inside.load() == 1);
    }
    CHECK(gate.inside.load() == 0);
}

TEST_CASE("hook unload: a detour's scope covers the call it makes before it returns") {
    // The detours end in `return original(...)`: the original runs, and returns into the detour, with the count up.
    static DwmHookCallGate gate;
    static long seenInOriginal = -1;
    struct Fn {
        static int Original(int v) { seenInOriginal = gate.inside.load(); return v + 1; }
        static int Detour(int v) {
            DwmHookCallScope inHook(gate);
            return Original(v);
        }
    };
    CHECK(Fn::Detour(41) == 42);
    CHECK(seenInOriginal == 1);
    CHECK(gate.inside.load() == 0);
}

TEST_CASE("hook unload drain: nobody inside is seen at once, with one frozen look") {
    FakeDrain d;
    d.insideSeq = { 0 };
    d.outsideSeq = { 1 };
    CHECK(DwmHookDrainHooks(d, 3000));
    CHECK(d.outsideCalls == 1);
    CHECK(d.pauses.empty());
}

TEST_CASE("hook unload drain: waits while a thread is inside a detour, and takes no frozen look meanwhile") {
    FakeDrain d;
    d.insideSeq = { 1, 1, 1, 2, 1, 0 };   // a present in flight (and a second detour entered from it), then out
    d.outsideSeq = { 1 };
    CHECK(DwmHookDrainHooks(d, 3000));
    CHECK(d.insideCalls == 6);
    CHECK(d.outsideCalls == 1);           // the process is frozen once, when the count already reads 0
    REQUIRE(d.insideAtLook.size() == 1);
    CHECK(d.insideAtLook[0] == 0);
    CHECK(d.pauses == std::vector<unsigned>{ 1, 1, 1, 1, 1 });
}

TEST_CASE("hook unload drain: a count of 0 is not enough while a thread sits in the hook's code") {
    // The count cannot cover a detour's first instructions, its last ones, or MinHook's relay jump.
    FakeDrain d;
    d.insideSeq = { 0 };
    d.outsideSeq = { 0, 0, 1 };
    CHECK(DwmHookDrainHooks(d, 3000));
    CHECK(d.outsideCalls == 3);
    CHECK(d.pauses == std::vector<unsigned>{ 2, 4 });   // a look that said no is not repeated at once
}

TEST_CASE("hook unload drain: a thread that never leaves ends in a refusal, never in 'clear'") {
    FakeDrain d;
    d.insideSeq = { 1 };
    d.outsideSeq = { 1 };
    CHECK_FALSE(DwmHookDrainHooks(d, 250));
    CHECK(d.outsideCalls == 0);
    CHECK(d.now - 1000 == 250);           // the whole budget, not more
    unsigned long long slept = 0;
    for (unsigned p : d.pauses) slept += p;
    CHECK(slept == 250);
}

TEST_CASE("hook unload drain: a look that could not be taken is never taken as 'outside'") {
    FakeDrain d;
    d.insideSeq = { 0 };
    d.outsideSeq = { -1 };
    CHECK_FALSE(DwmHookDrainHooks(d, 500));
    // Spaced out: 2, 4, 8, 16, 32, 64, 64 .. and the last pause cut to what is left of the budget.
    REQUIRE(d.pauses.size() >= 7);
    CHECK(d.pauses[0] == 2);
    CHECK(d.pauses[5] == 64);
    CHECK(d.pauses[6] == 64);
    CHECK(d.now - 1000 == 500);
    CHECK(d.outsideCalls < 20);           // not a freeze of the whole process every millisecond
}

TEST_CASE("hook unload drain: a zero budget still takes its one look") {
    FakeDrain d;
    d.insideSeq = { 0 };
    d.outsideSeq = { 1 };
    CHECK(DwmHookDrainHooks(d, 0));
    FakeDrain busy;
    busy.insideSeq = { 3 };
    busy.outsideSeq = { 1 };
    CHECK_FALSE(DwmHookDrainHooks(busy, 0));
    CHECK(busy.pauses.empty());
}

TEST_CASE("hook unload: address ranges are half open") {
    const DwmHookCodeRange r[2] = { { 0x1000, 0x2000 }, { 0x9000, 0x9040 } };
    CHECK_FALSE(DwmHookAddressInRanges(0x0FFF, r, 2));
    CHECK(DwmHookAddressInRanges(0x1000, r, 2));
    CHECK(DwmHookAddressInRanges(0x1FFF, r, 2));
    CHECK_FALSE(DwmHookAddressInRanges(0x2000, r, 2));
    CHECK(DwmHookAddressInRanges(0x903F, r, 2));
    CHECK_FALSE(DwmHookAddressInRanges(0x9040, r, 2));
    CHECK_FALSE(DwmHookAddressInRanges(0x1000, r, 0));
}

TEST_CASE("hook unload: the frozen look finds a thread whose instruction pointer is in the code, count 0 or not") {
    SpinThread spin;
    REQUIRE(spin.Start());
    // A page nothing runs in.
    void* idle = VirtualAlloc(nullptr, 4096, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    REQUIRE(idle != nullptr);
    const DwmHookCodeRange idleRange = { reinterpret_cast<uintptr_t>(idle), reinterpret_cast<uintptr_t>(idle) + 4096 };
    const DwmHookCodeRange spinRange = spin.Range();
    const DWORD spinTid = GetThreadId(spin.thread);

    DwmHookCallGate gate;   // reads 0: this is the case only the look can catch
    DwmHookLook look;
    CHECK(LookSettled(&spinRange, 1, &gate, &look) == 0);
    CHECK(look.insideTid == spinTid);            // the refusal's log line names the thread and where it stands
    CHECK(look.insideIp >= spinRange.begin);
    CHECK(look.insideIp < spinRange.end);
    CHECK(look.count == 0);
    CHECK(look.threads >= 1);
    CHECK(LookSettled(&idleRange, 1, &gate, &look) == 1);
    CHECK(look.insideTid == 0);
    CHECK(look.unseen == DwmHookUnseen::None);
    const DwmHookCodeRange both[2] = { idleRange, spinRange };
    CHECK(LookSettled(both, 2, &gate) == 0);
    // The look resumes what it suspended: the thread still runs and still ends when told to.
    spin.Stop();
    CHECK(LookSettled(&spinRange, 1, &gate) == 1);

    // A count that is up when everything stands still is a thread inside, wherever its instruction pointer is.
    {
        DwmHookCallScope held(gate);
        CHECK(LookSettled(&idleRange, 1, &gate, &look) == 0);
        CHECK(look.count == 1);
        CHECK(look.insideTid == 0);
    }
    CHECK(LookSettled(&idleRange, 1, &gate) == 1);
    VirtualFree(idle, 0, MEM_RELEASE);
}

TEST_CASE("hook unload: the look never suspends the thread that takes it, and leaves every thread running") {
    // Many looks in a row from this thread while another one counts: neither may be left suspended.
    struct Counter {
        std::atomic<bool> stop{ false };
        std::atomic<unsigned long long> ticks{ 0 };
        static DWORD WINAPI Run(LPVOID p) {
            auto* c = static_cast<Counter*>(p);
            while (!c->stop.load()) c->ticks.fetch_add(1);
            return 0;
        }
    } counter;
    HANDLE t = CreateThread(nullptr, 0, Counter::Run, &counter, 0, nullptr);
    REQUIRE(t != nullptr);
    DwmHookCallGate gate;
    const DwmHookCodeRange nowhere = { 1, 2 };
    for (int i = 0; i < 50; i++) CHECK(LookSettled(&nowhere, 1, &gate) == 1);
    const unsigned long long before = counter.ticks.load();
    while (counter.ticks.load() == before) SwitchToThread();   // it still runs (no deadline: it will)
    counter.stop.store(true);
    WaitForSingleObject(t, INFINITE);
    CloseHandle(t);
}

TEST_CASE("hook unload: the real drain refuses while a thread is in the code and clears once it has left") {
    struct Ops {
        DwmHookCallGate gate;
        DwmHookCodeRange range;
        long Inside() { return gate.inside.load(); }
        int ThreadsOutside() { return DwmHookThreadsOutside(&range, 1, &gate); }
        unsigned long long NowMs() { return GetTickCount64(); }
        void Pause(unsigned ms) { Sleep(ms); }
    } ops;
    SpinThread spin;
    REQUIRE(spin.Start());
    ops.range = spin.Range();
    CHECK_FALSE(DwmHookDrainHooks(ops, 40));   // in the code for as long as anyone waits: refused
    spin.Stop();
    CHECK(DwmHookDrainHooks(ops, 60000));      // gone: seen at the first look that could be taken
}

TEST_CASE("hook unload: the request and busy events are named per dwm.exe") {
    wchar_t request[96], busy[96], other[96];
    DwmHookUnloadRequestEventName(25528, request, _countof(request));
    DwmHookUnloadBusyEventName(25528, busy, _countof(busy));
    DwmHookUnloadRequestEventName(2400, other, _countof(other));
    CHECK(std::wstring(request) == L"Global\\DesktopLUT_DwmHook_Unload_25528");
    CHECK(std::wstring(busy) == L"Global\\DesktopLUT_DwmHook_UnloadBusy_25528");
    CHECK(std::wstring(request) != std::wstring(busy));
    CHECK(std::wstring(request) != std::wstring(other));   // another session's dwm.exe is another DLL
}

TEST_CASE("hook unload: what the host makes of one look at dwm.exe while it waits") {
    // The module list is what is true: gone is gone, whatever was signalled. But not on one reading: the module
    // leaves the list a moment before its image is unmapped, and a list read while it changes can come back short.
    {
        DwmHookEjectWait w;
        CHECK(DwmHookEjectPoll(w, DwmHookModule::Gone, false) == DwmHookEjectStep::KeepWaiting);
        CHECK(DwmHookEjectPoll(w, DwmHookModule::Gone, false) == DwmHookEjectStep::Unloaded);
    }
    {
        DwmHookEjectWait w;   // gone twice wins over a refusal that was signalled
        CHECK(DwmHookEjectPoll(w, DwmHookModule::Gone, true) == DwmHookEjectStep::KeepWaiting);
        CHECK(DwmHookEjectPoll(w, DwmHookModule::Gone, true) == DwmHookEjectStep::Unloaded);
    }
    {
        DwmHookEjectWait w;   // a short list once, then the module is there again: the count starts over
        CHECK(DwmHookEjectPoll(w, DwmHookModule::Gone, false) == DwmHookEjectStep::KeepWaiting);
        CHECK(DwmHookEjectPoll(w, DwmHookModule::Loaded, false) == DwmHookEjectStep::KeepWaiting);
        CHECK(DwmHookEjectPoll(w, DwmHookModule::Gone, false) == DwmHookEjectStep::KeepWaiting);
        CHECK(DwmHookEjectPoll(w, DwmHookModule::Unreadable, false) == DwmHookEjectStep::KeepWaiting);
        CHECK(DwmHookEjectPoll(w, DwmHookModule::Gone, false) == DwmHookEjectStep::KeepWaiting);
        // ... and a refusal that arrives after a short reading is still a refusal
        CHECK(DwmHookEjectPoll(w, DwmHookModule::Loaded, true) == DwmHookEjectStep::StillLoaded);
    }
    DwmHookEjectWait w;
    // Still there and the DLL said it stays: the host stops waiting and stages nothing over it.
    CHECK(DwmHookEjectPoll(w, DwmHookModule::Loaded, true) == DwmHookEjectStep::StillLoaded);
    CHECK(DwmHookEjectPoll(w, DwmHookModule::Unreadable, true) == DwmHookEjectStep::StillLoaded);
    // Still there, nothing said yet: the drain and the teardown are at work.
    CHECK(DwmHookEjectPoll(w, DwmHookModule::Loaded, false) == DwmHookEjectStep::KeepWaiting);
    // A list that could not be read is never "gone" (a snapshot fails while a module unloads), however often.
    for (int i = 0; i < 10; i++)
        CHECK(DwmHookEjectPoll(w, DwmHookModule::Unreadable, false) == DwmHookEjectStep::KeepWaiting);
    // The host outwaits the DLL: the DLL's join of its monitor thread and its drain (the same budget each) must fit
    // inside the host's wait.
    CHECK(2 * kDwmHookUnloadDrainMs + 1000 <= kDwmHookUnloadHostMs);
    CHECK(kDwmHookDetachDrainMs < kDwmHookUnloadDrainMs);
    CHECK(kDwmHookNoEventRecheckMs >= 10 * kDwmHookUnloadPollMs);
}
