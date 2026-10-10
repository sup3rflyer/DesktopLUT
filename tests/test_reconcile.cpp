#include "doctest.h"
#include "actions/reconcile.h"

// ============================================================================
// Live layers follow the configuration (src/actions/reconcile.*): DecideReconcile is the whole policy —
// configured means running, Suspend wins, nothing starts or stops while something else owns the pipeline.
// ============================================================================

using act::DecideReconcile;
using act::ReconcileDecision;
using act::ReconcileInputs;

namespace {
ReconcileInputs In(bool running, bool needed, bool suspended = false, bool busy = false, bool hookStale = false,
                   bool shuttingDown = false) {
    ReconcileInputs in;
    in.running = running;
    in.needed = needed;
    in.suspended = suspended;
    in.busy = busy;
    in.hookStale = hookStale;
    in.shuttingDown = shuttingDown;
    return in;
}
}  // namespace

TEST_CASE("reconcile: configured means running") {
    CHECK(DecideReconcile(In(false, true)) == ReconcileDecision::Start);
    CHECK(DecideReconcile(In(true, false)) == ReconcileDecision::Stop);
    CHECK(DecideReconcile(In(true, true)) == ReconcileDecision::None);
    CHECK(DecideReconcile(In(false, false)) == ReconcileDecision::None);
}

TEST_CASE("reconcile: a resident hook with another display list restarts, only while needed and running") {
    CHECK(DecideReconcile(In(true, true, false, false, true)) == ReconcileDecision::Restart);
    CHECK(DecideReconcile(In(false, true, false, false, true)) == ReconcileDecision::Start);
    CHECK(DecideReconcile(In(true, false, false, false, true)) == ReconcileDecision::Stop);
    CHECK(DecideReconcile(In(true, true, true, false, true)) == ReconcileDecision::Stop);   // suspended
}

TEST_CASE("reconcile: Suspend keeps the live layers stopped whatever is configured") {
    CHECK(DecideReconcile(In(false, true, true)) == ReconcileDecision::None);
    CHECK(DecideReconcile(In(true, true, true)) == ReconcileDecision::Stop);
    CHECK(DecideReconcile(In(true, false, true)) == ReconcileDecision::Stop);
    CHECK(DecideReconcile(In(false, false, true)) == ReconcileDecision::None);
}

TEST_CASE("reconcile: busy defers every change, shutdown does nothing") {
    for (int bits = 0; bits < 16; bits++) {
        const bool running = bits & 1, needed = bits & 2, suspended = bits & 4, stale = bits & 8;
        CAPTURE(bits);
        CHECK(DecideReconcile(In(running, needed, suspended, true, stale)) == ReconcileDecision::Defer);
        CHECK(DecideReconcile(In(running, needed, suspended, true, stale, true)) == ReconcileDecision::None);
        CHECK(DecideReconcile(In(running, needed, suspended, false, stale, true)) == ReconcileDecision::None);
    }
}

TEST_CASE("reconcile: the decision is a fixed point once carried out") {
    // After Start / Stop / Restart the state matches the configuration: reconciling again does nothing.
    for (int bits = 0; bits < 8; bits++) {
        const bool running = bits & 1, needed = bits & 2, suspended = bits & 4;
        CAPTURE(bits);
        const ReconcileDecision d = DecideReconcile(In(running, needed, suspended, false, true));
        bool after = running;
        if (d == ReconcileDecision::Start || d == ReconcileDecision::Restart) after = true;
        if (d == ReconcileDecision::Stop) after = false;
        CHECK(DecideReconcile(In(after, needed, suspended)) == ReconcileDecision::None);
    }
}
