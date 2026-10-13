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

// ============================================================================
// Hook staging (act::DecideHookStale): the DWM hook reads its staged files (cubes, FALD panel files) only at
// injection and matches them to displays by desktop position; the tone mapper and the FALD switches reach a
// resident hook through the shared config, for every display. The owner's rig: the PA32UCXR at (0,0), the
// LG TV (powered on after boot, off and on again) or a BenQ swapped onto its connector at (3840,0).
// ============================================================================

using act::DecideHookStale;
using act::DisplayPosition;
using act::HookStaging;

namespace {

const DisplayPosition kPa{ 0, 0 };
const DisplayPosition kTv{ 3840, 0 };   // the second connector: the LG, or a BenQ swapped onto it

DwmHookMonitorLUT Entry(DisplayPosition at, const wchar_t* sdrCube = L"", const wchar_t* hdrCube = L"",
                        const wchar_t* sdrPanel = L"", const wchar_t* hdrPanel = L"") {
    DwmHookMonitorLUT e;
    e.left = at.left;
    e.top = at.top;
    e.sdrLutPath = sdrCube;
    e.hdrLutPath = hdrCube;
    e.sdrFaldPath = sdrPanel;
    e.hdrFaldPath = hdrPanel;
    return e;
}

const wchar_t* kPanel = L"C:\\panels\\fald_panel.bin";
const DwmHookMonitorLUT kPaCube = Entry(kPa, L"C:\\luts\\pa_sdr.cube", L"C:\\luts\\pa_hdr.cube");
const DwmHookMonitorLUT kTvCube = Entry(kTv, L"", L"C:\\luts\\lg_hdr.cube");
const DwmHookMonitorLUT kTvToneMapOnly = Entry(kTv);   // the HDR tone mapper is its only hook work: no file
const DwmHookMonitorLUT kTvFaldOnly = Entry(kTv, L"", L"", L"", kPanel);   // layer on or off: the file is staged

// A resident hook this process injected with `injected`, identified live displays at `identified`, the
// configuration wanting `desired`, and a restart that starts again (some display needs processing).
HookStaging Resident(std::vector<DwmHookMonitorLUT> injected, std::vector<DwmHookMonitorLUT> desired,
                     std::vector<DisplayPosition> identified) {
    HookStaging s;
    s.hookActive = true;
    s.injectedKnown = true;
    s.restartInjects = true;
    s.injected = std::move(injected);
    s.desired = std::move(desired);
    s.identified = std::move(identified);
    return s;
}

// After a restart that starts again: the hook injected with what was desired (nothing when that is empty).
HookStaging AfterReinject(const HookStaging& before) {
    REQUIRE(before.restartInjects);
    HookStaging s = before;
    s.hookActive = !before.desired.empty();
    s.startStagedNothing = before.desired.empty();
    s.injected = before.desired;
    return s;
}

}  // namespace

TEST_CASE("hook staging: a hook injected with what the displays want stays") {
    CHECK_FALSE(DecideHookStale(Resident({ kPaCube, kTvCube }, { kPaCube, kTvCube }, { kPa, kTv })));
    CHECK_FALSE(DecideHookStale(Resident({ kTvCube, kPaCube }, { kPaCube, kTvCube }, { kPa, kTv })));   // any order
    CHECK_FALSE(DecideHookStale(Resident({ kPaCube, kTvFaldOnly }, { kPaCube, kTvFaldOnly }, { kPa, kTv })));
    CHECK_FALSE(DecideHookStale(Resident({}, {}, { kPa, kTv })));
}

// C17 follow-up: the topology tick restarted only for a .cube (SettingsChanged), and layers.set only pushed
// the shared config, so a late display's FALD panel file was never staged while another display kept the
// pipeline running. Its tone mapper needs no staging: the shared config lists every display.
TEST_CASE("hook staging: a late display's FALD panel file re-stages; its tone mapper rides the shared config") {
    const HookStaging fald = Resident({ kPaCube }, { kPaCube, kTvFaldOnly }, { kPa, kTv });
    CHECK(DecideHookStale(fald));
    CHECK_FALSE(DecideHookStale(AfterReinject(fald)));
    // Also the state after layers.set switches the tone mapper on for a display the hook was not injected with.
    CHECK_FALSE(DecideHookStale(Resident({ kPaCube }, { kPaCube, kTvToneMapOnly }, { kPa, kTv })));
    // A late display's cube.
    const HookStaging cube = Resident({ kPaCube }, { kPaCube, kTvCube }, { kPa, kTv });
    CHECK(DecideHookStale(cube));
    CHECK_FALSE(DecideHookStale(AfterReinject(cube)));
}

TEST_CASE("hook staging: the running start injected nothing and now a display has hook work") {
    HookStaging s;   // the pipeline runs for overlay work only (a shader tweak, desktop gamma): no hook resident
    s.hookActive = false;
    s.startStagedNothing = true;
    s.restartInjects = true;
    s.identified = { kPa, kTv };
    CHECK_FALSE(DecideHookStale(s));
    s.desired = { kTvToneMapOnly };   // tone mapping too: it needs the hook resident
    CHECK(DecideHookStale(s));
    CHECK_FALSE(DecideHookStale(AfterReinject(s)));
}

// The pipeline also runs while nothing needs processing: desktop gamma configured but toggled off by its
// hotkey (the reconcile keeps the hotkey alive), or a display that left (the topology tick never stops). A
// restart then starts nothing again (and drops the hotkey): it is worth it only to take a wrong cube away.
TEST_CASE("hook staging: a restart that would inject nothing again adds no hook work") {
    HookStaging never;   // no hook resident; a display with a panel file (layer off) arrives
    never.hookActive = false;
    never.startStagedNothing = true;
    never.desired = { kTvFaldOnly };
    never.identified = { kPa, kTv };
    CHECK_FALSE(DecideHookStale(never));

    HookStaging lacks = Resident({ kPaCube }, { kPaCube, kTvFaldOnly }, { kPa, kTv });
    lacks.restartInjects = false;   // (in practice the PA's cube needs processing; the rule alone)
    CHECK_FALSE(DecideHookStale(lacks));

    HookStaging wrongCube = Resident({ kTvCube }, {}, { kPa, kTv });   // a BenQ without hook work under the cube
    wrongCube.restartInjects = false;
    CHECK(DecideHookStale(wrongCube));
}

TEST_CASE("hook staging: a hook that is gone is the watchdog's") {
    HookStaging s;   // injected at start, then lost (DWM restarted, or the watchdog gave up re-injecting)
    s.hookActive = false;
    s.startStagedNothing = false;
    s.restartInjects = true;
    s.desired = { kPaCube, kTvToneMapOnly };
    s.identified = { kPa, kTv };
    CHECK_FALSE(DecideHookStale(s));
}

TEST_CASE("hook staging: a resident hook whose staging set is unknown stays") {
    HookStaging s = Resident({}, { kPaCube, kTvCube }, { kPa, kTv });
    s.injectedKnown = false;   // a failed inject attempt, or a hook this process did not inject
    CHECK_FALSE(DecideHookStale(s));
}

TEST_CASE("hook staging: a display the attachment change moved re-stages its files (matched by position)") {
    // The layout shifted: the second display now starts at 2560 (another display's width changed before it).
    const DisplayPosition moved{ 2560, 0 };
    DwmHookMonitorLUT tvMoved = kTvCube;
    tvMoved.left = moved.left;
    const HookStaging s = Resident({ kPaCube, kTvCube }, { kPaCube, tvMoved }, { kPa, moved });
    CHECK(DecideHookStale(s));
    CHECK_FALSE(DecideHookStale(AfterReinject(s)));
    // Its tone mapper alone moves with it through the shared config.
    DwmHookMonitorLUT tmMoved = kTvToneMapOnly;
    tmMoved.left = moved.left;
    CHECK_FALSE(DecideHookStale(Resident({ kPaCube, kTvToneMapOnly }, { kPaCube, tmMoved }, { kPa, moved })));
}

TEST_CASE("hook staging: the TV powering off and on costs no re-inject") {
    // Off: its settings are parked, so nothing wants its entry; nothing sits at its position either.
    CHECK_FALSE(DecideHookStale(Resident({ kPaCube, kTvCube }, { kPaCube }, { kPa })));
    CHECK_FALSE(DecideHookStale(Resident({ kPaCube, kTvToneMapOnly }, { kPaCube }, { kPa })));
    CHECK_FALSE(DecideHookStale(Resident({ kTvCube }, {}, { kPa })));   // the TV was the only display with hook work
    // On again at the same position with the same settings: the resident entry is already right.
    CHECK_FALSE(DecideHookStale(Resident({ kPaCube, kTvCube }, { kPaCube, kTvCube }, { kPa, kTv })));
    // On again, its identity not read yet (default settings, no identity): it is most likely the TV itself,
    // so its cube stays until the identity says otherwise.
    CHECK_FALSE(DecideHookStale(Resident({ kPaCube, kTvCube }, { kPaCube }, { kPa })));
    // On again elsewhere: it moved.
    DwmHookMonitorLUT tvLeft = kTvCube;
    tvLeft.left = -3840;
    CHECK(DecideHookStale(Resident({ kPaCube, kTvCube }, { kPaCube, tvLeft }, { kPa, { -3840, 0 } })));
}

TEST_CASE("hook staging: a cube under a display swapped onto the connector re-stages") {
    // A BenQ with no hook work of its own on the TV's connector: the hook would show it through the TV's cube.
    const HookStaging cube = Resident({ kPaCube, kTvCube }, { kPaCube }, { kPa, kTv });
    CHECK(DecideHookStale(cube));
    CHECK_FALSE(DecideHookStale(AfterReinject(cube)));
    // No other display has hook work either: the re-inject stages nothing and the hook leaves.
    const HookStaging alone = Resident({ kTvCube }, {}, { kPa, kTv });
    CHECK(DecideHookStale(alone));
    CHECK_FALSE(DecideHookStale(AfterReinject(alone)));
    // The BenQ has hook work of its own (tone mapping, another cube): the TV's cube is still not its.
    CHECK(DecideHookStale(Resident({ kPaCube, kTvCube }, { kPaCube, kTvToneMapOnly }, { kPa, kTv })));
    CHECK(DecideHookStale(Resident({ kPaCube, kTvCube }, { kPaCube, Entry(kTv, L"C:\\luts\\benq.cube") },
                                   { kPa, kTv })));
    // The TV's panel file under the BenQ is inert: the layer runs only where the BenQ's own settings switch it
    // on, with its own file (which the hook would then lack).
    CHECK_FALSE(DecideHookStale(Resident({ kPaCube, kTvFaldOnly }, { kPaCube }, { kPa, kTv })));
    CHECK(DecideHookStale(Resident({ kPaCube, kTvFaldOnly },
                                   { kPaCube, Entry(kTv, L"", L"", L"", L"C:\\panels\\benq.bin") }, { kPa, kTv })));
}

TEST_CASE("hook staging: a display that only lost a live switch or a file it no longer uses keeps the hook") {
    // Tone mapping off on a display it was the only hook work of: the entry staged no file.
    CHECK_FALSE(DecideHookStale(Resident({ kPaCube, kTvToneMapOnly }, { kPaCube }, { kPa, kTv })));
    // The FALD layer off: its panel file stays staged (and desired), the switch rides the shared config.
    CHECK_FALSE(DecideHookStale(Resident({ kPaCube, kTvFaldOnly }, { kPaCube, kTvFaldOnly }, { kPa, kTv })));
    // A panel file no longer configured beside the same cube: inert (the switch needs the file).
    const DwmHookMonitorLUT cubeAndPanel = Entry(kTv, L"", L"C:\\luts\\lg_hdr.cube", L"", kPanel);
    CHECK_FALSE(DecideHookStale(Resident({ kPaCube, cubeAndPanel }, { kPaCube, kTvCube }, { kPa, kTv })));
    // ...but a panel file added beside it is one the hook lacks.
    CHECK(DecideHookStale(Resident({ kPaCube, kTvCube }, { kPaCube, cubeAndPanel }, { kPa, kTv })));
}

// ============================================================================
// Topology tick (act::DecideTopologyTick, TOPOLOGY_PIPELINE_TIMER_ID)
// ============================================================================

using act::DecideTopologyTick;
using act::TopologyInputs;

namespace {
TopologyInputs Topo(bool running, bool needed, bool suspended = false, bool lutsChanged = false,
                    bool hookStale = false, bool busy = false) {
    TopologyInputs in;
    in.running = running;
    in.needed = needed;
    in.suspended = suspended;
    in.lutsChanged = lutsChanged;
    in.hookStale = hookStale;
    in.busy = busy;
    return in;
}
}  // namespace

TEST_CASE("topology tick: never stops (a display that left keeps what runs for it)") {
    for (int bits = 0; bits < 32; bits++) {
        const bool running = bits & 1, needed = bits & 2, suspended = bits & 4, luts = bits & 8, stale = bits & 16;
        CAPTURE(bits);
        CHECK(DecideTopologyTick(Topo(running, needed, suspended, luts, stale)) != ReconcileDecision::Stop);
    }
    // The TV powered off and nothing else needs processing: the pipeline keeps running for its return.
    CHECK(DecideTopologyTick(Topo(true, false)) == ReconcileDecision::None);
}

TEST_CASE("topology tick: not running starts what a live display needs, unless suspended") {
    CHECK(DecideTopologyTick(Topo(false, true)) == ReconcileDecision::Start);
    CHECK(DecideTopologyTick(Topo(false, true, true)) == ReconcileDecision::None);
    CHECK(DecideTopologyTick(Topo(false, false)) == ReconcileDecision::None);
    CHECK(DecideTopologyTick(Topo(false, false, false, true, true)) == ReconcileDecision::None);
}

TEST_CASE("topology tick: running restarts for a LUT it lacks or a stale hook") {
    CHECK(DecideTopologyTick(Topo(true, true)) == ReconcileDecision::None);
    CHECK(DecideTopologyTick(Topo(true, true, false, true)) == ReconcileDecision::Restart);
    CHECK(DecideTopologyTick(Topo(true, true, false, false, true)) == ReconcileDecision::Restart);
    // A cube under a swapped-in display while nothing needs processing: the restart takes the hook away.
    CHECK(DecideTopologyTick(Topo(true, false, false, false, true)) == ReconcileDecision::Restart);
    // Suspended while something runs: the restart's Stop stands (RestartLiveLayers does not start again).
    CHECK(DecideTopologyTick(Topo(true, true, true, false, true)) == ReconcileDecision::Restart);
}

TEST_CASE("topology tick: busy waits") {
    for (int bits = 0; bits < 32; bits++) {
        const bool running = bits & 1, needed = bits & 2, suspended = bits & 4, luts = bits & 8, stale = bits & 16;
        CAPTURE(bits);
        CHECK(DecideTopologyTick(Topo(running, needed, suspended, luts, stale, true)) == ReconcileDecision::Defer);
    }
}
