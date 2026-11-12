// DesktopLUT - dwm_hook_stability.h
// When the DWM hook has proved unusable on this system, so that hook mode falls back to the overlay
// (gui.cpp HookUnusableFallBackToOverlay: hook mode off and saved, the overlay takes the live layers over).
// Header-only so the tests drive the same rules the GUI thread's watchdog and start path use.
//
// Three rules, each ending in that fall-back:
//   - the FIRST injection fails (a start, not the watchdog): once more after a short wait, then fall back.
//     One retry covers a dwm.exe that was restarting at that moment; a DLL missing, refused or unable to find
//     its patterns fails the same way twice.
//   - fast flapping (the flap cap, unchanged since 2.x): `maxReinjects` re-injections without a healthy streak
//     of kHealthyTicksToForgive watchdog ticks between them. A start re-arms it.
//   - a slow loop: kSlowLoopDetaches detaches within kSlowLoopWindowMs, or kDayLoopDetaches within
//     kDayLoopWindowMs. A hook that dies every 40 s builds a healthy streak between detaches, so the flap cap
//     never saw it and it was re-injected (a black flash each time) forever. These windows survive restarts of
//     the live layers; only the user switching DWM HOOK on again clears them (the deliberate retry, after a
//     Windows update say). An isolated detach (a Windows update or a driver install restarting dwm.exe once or
//     twice) stays far below either count.
// GUI thread only (the watchdog timer, the start path's posted failure, the settings action).

#pragma once

#include <cstdint>

enum class HookStartFailAction { Retry, FallBack };

struct DwmHookStability {
    static constexpr int kHealthyTicksToForgive = 6;              // watchdog ticks (5 s): 30 s attached
    static constexpr int kSlowLoopDetaches = 6;
    static constexpr uint64_t kSlowLoopWindowMs = 2ull * 3600 * 1000;    // 6 detaches within 2 hours
    static constexpr int kDayLoopDetaches = 10;
    static constexpr uint64_t kDayLoopWindowMs = 24ull * 3600 * 1000;    // 10 detaches within 24 hours
    static constexpr int kHistory = kDayLoopDetaches;             // the newest detach times kept

    int reinjects = 0;          // the watchdog's re-injection cycles since the last start or healthy streak
    int healthyStreak = 0;      // consecutive healthy watchdog ticks while reinjects > 0
    bool gaveUp = false;        // a give-up happened since the last start (the hook will not come back by itself)
    int startFailures = 0;      // first injections that failed in a row
    uint64_t detachMs[kHistory] = {};   // ring of the newest detach times (ms, monotonic)
    int detachCount = 0;        // detaches recorded (saturates at kHistory)
    int detachNext = 0;         // ring write position

    // A hook-mode start of the live layers went through (its injection, if it had one, succeeded) and re-armed
    // the watchdog: the flap cap counts afresh (as before), a pending start retry is over. The slow-loop history
    // stays: a restart of the live layers is not a reason to forget a hook that keeps dying.
    void OnStart() {
        reinjects = 0;
        healthyStreak = 0;
        gaveUp = false;
        startFailures = 0;
    }

    // The first injection of a start failed: retry once, then fall back.
    HookStartFailAction OnStartFailed() {
        ++startFailures;
        if (startFailures >= 2) {
            gaveUp = true;
            return HookStartFailAction::FallBack;
        }
        return HookStartFailAction::Retry;
    }

    // A healthy watchdog tick: a sustained stretch attached forgives the flap counter (a true flap re-injects
    // on every tick and never builds a streak this long).
    void OnHealthyTick() {
        if (reinjects > 0 && ++healthyStreak >= kHealthyTicksToForgive) {
            reinjects = 0;
            healthyStreak = 0;
        } else if (reinjects == 0) {
            healthyStreak = 0;
        }
    }

    // The watchdog re-injected successfully.
    void OnReinjected() { ++reinjects; }

    bool FlapCapReached(int maxReinjects) const { return reinjects >= maxReinjects; }

    // Detaches recorded within `windowMs` before `nowMs` (inclusive of nowMs).
    int DetachesWithin(uint64_t nowMs, uint64_t windowMs) const {
        int n = 0;
        for (int i = 0; i < detachCount; ++i) {
            const uint64_t t = detachMs[i];
            if (t <= nowMs && nowMs - t < windowMs) ++n;
        }
        return n;
    }

    // The watchdog found the hook gone while a display had work for it (once per detach: not for the retries
    // of a failed re-injection). True = the slow-loop rule gives up: fall back instead of re-injecting.
    bool OnDetach(uint64_t nowMs) {
        detachMs[detachNext] = nowMs;
        detachNext = (detachNext + 1) % kHistory;
        if (detachCount < kHistory) ++detachCount;
        const bool giveUp = DetachesWithin(nowMs, kSlowLoopWindowMs) >= kSlowLoopDetaches ||
                            DetachesWithin(nowMs, kDayLoopWindowMs) >= kDayLoopDetaches;
        if (giveUp) gaveUp = true;
        return giveUp;
    }

    // Any give-up of the watchdog (the flap cap, failed re-injections).
    void OnGiveUp() { gaveUp = true; }

    // The user switched DWM HOOK on: every count starts again.
    void OnUserReEnable() { *this = DwmHookStability(); }
};
