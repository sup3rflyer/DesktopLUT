// The hub window's own decisions (gui.cpp): what the interface shows of the DWM hook's twin routing.
#include "doctest.h"
#include "gui.h"
#include "dwm_inject.h"

TEST_CASE("hub: the routing file's methods read as words") {
    CHECK(HookRoutingMethodLabel("beacon") == L"identified");
    CHECK(HookRoutingMethodLabel("unique") == L"unambiguous");
    CHECK(HookRoutingMethodLabel("bpc") == L"unambiguous");
    CHECK(HookRoutingMethodLabel("legacy") == L"unambiguous");
    CHECK(HookRoutingMethodLabel("order") == L"guessed");
    CHECK(HookRoutingMethodLabel("replaced") == L"inferred");
    CHECK(HookRoutingMethodLabel("pinned") == L"pinned");   // anything else as written
    CHECK(HookRoutingMethodLabel("") == L"");
}

TEST_CASE("hub: the twin routing view") {
    const std::vector<POINT> pos = { { 0, 0 }, { 3840, 0 }, { -1920, 0 } };
    DwmHookRouting r;
    r.present = true;
    auto entry = [](const char* ctx, int left, int top, const char* method) {
        DwmHookRoutingEntry e;
        e.ctx = ctx; e.left = left; e.top = top; e.method = method;
        return e;
    };
    r.entries = { entry("a1", 0, 0, "beacon"), entry("b2", 3840, 0, "order"), entry("c3", 3840, 0, "order"),
                  entry("d4", 3840, 0, "beacon"), entry("e5", -1920, 0, "unique") };

    SUBCASE("no twins: nothing to show, whatever the file says") {
        const HookRoutingView v = BuildHookRoutingView({}, pos, r);
        CHECK(v.twins.empty());
    }
    SUBCASE("one pair: each display's methods, once each, in the file's order") {
        const HookRoutingView v = BuildHookRoutingView({ { 0, 1 } }, pos, r);
        REQUIRE(v.twins.size() == 2);
        CHECK(!v.fileStale);
        CHECK(v.twins[0].monitor == 0);
        CHECK(v.twins[0].group == 0);
        CHECK(v.twins[0].how == L"identified");
        CHECK(v.twins[1].monitor == 1);
        CHECK(v.twins[1].how == L"guessed/identified");
    }
    SUBCASE("a stale file (dwm.exe restarted) assigns nothing") {
        r.stale = true;
        const HookRoutingView v = BuildHookRoutingView({ { 0, 1 } }, pos, r);
        REQUIRE(v.twins.size() == 2);
        CHECK(v.fileStale);
        CHECK(v.twins[0].how.empty());
        CHECK(v.twins[1].how.empty());
    }
    SUBCASE("no file: the twins are listed, unassigned") {
        const HookRoutingView v = BuildHookRoutingView({ { 0, 1 } }, pos, DwmHookRouting());
        REQUIRE(v.twins.size() == 2);
        CHECK(v.fileStale);
        CHECK(v.twins[1].how.empty());
    }
    SUBCASE("two groups keep their numbers; a monitor index without a position is listed, unassigned") {
        const HookRoutingView v = BuildHookRoutingView({ { 0, 1 }, { 2, 7 } }, pos, r);
        REQUIRE(v.twins.size() == 4);
        CHECK(v.twins[2].group == 1);
        CHECK(v.twins[2].how == L"unambiguous");
        CHECK(v.twins[3].monitor == 7);
        CHECK(v.twins[3].how.empty());
    }
}
