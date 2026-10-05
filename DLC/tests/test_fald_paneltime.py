"""The measured panel time law (dlc.fald.paneltime): 30-Hz sample-and-hold LEDs, compensation one frame later."""
import math

import numpy as np
import pytest

from dlc.fald.paneltime import PanelClock, PanelTimeLaw


def _run(parity, n=10, law=PanelTimeLaw(closure=0.75)):
    c = PanelClock(law, parity)
    seq = [np.array([[0.2]])] * 3 + [np.array([[1.0]])] * n          # the LCD data changes at frame 3
    return [tuple(float(v[0, 0]) for v in c.step(d)) for d in seq]


@pytest.mark.parametrize("parity", [0, 1])
def test_first_led_step_is_one_or_two_frames_after_the_data(parity):
    tr = _run(parity)
    first = next(i for i, (s, _) in enumerate(tr) if s > 0.2 + 1e-12)
    assert first - 3 in (1, 2)
    assert {next(i for i, (s, _) in enumerate(_run(p)) if s > 0.2 + 1e-12) - 3 for p in (0, 1)} == {1, 2}


@pytest.mark.parametrize("parity", [0, 1])
def test_gap_closure_per_tick_and_hold_between_ticks(parity):
    tr = _run(parity)
    first = next(i for i, (s, _) in enumerate(tr) if s > 0.2 + 1e-12)
    s = [v for v, _ in tr]
    assert s[first] == pytest.approx(0.2 + 0.75 * 0.8)
    assert s[first + 1] == s[first]                                   # held until the next tick
    assert s[first + 2] == pytest.approx(s[first] + 0.75 * (1.0 - s[first]))
    assert all(b >= a for a, b in zip(s, s[1:]))                      # monotone, no overshoot
    assert max(s) <= 1.0


@pytest.mark.parametrize("parity", [0, 1])
def test_compensation_follows_the_led_state_one_frame_later(parity):
    tr = _run(parity)
    for i in range(1, len(tr)):
        assert tr[i][1] == tr[i - 1][0]
    first = next(i for i, (s, _) in enumerate(tr) if s > 0.2 + 1e-12)
    assert tr[first][0] > tr[first][1]                                # the flash frame: LEDs up, compensation not yet


def test_peek_is_the_state_step_returns_and_needs_no_current_frame():
    c = PanelClock(PanelTimeLaw(), 1)
    assert c.peek() is None
    rng = np.random.default_rng(1)
    for _ in range(8):
        d = rng.random((2, 3))
        pk = c.peek()
        got = c.step(d)
        if pk is not None:
            np.testing.assert_array_equal(pk[0], got[0])
            np.testing.assert_array_equal(pk[1], got[1])


def test_static_content_is_settled_from_the_first_frame():
    c = PanelClock(PanelTimeLaw(), 0)
    d = np.full((2, 2), 0.4)
    for _ in range(5):
        s, e = c.step(d)
        np.testing.assert_allclose(s, d)
        np.testing.assert_allclose(e, d)


def test_drive_state_known_parity_equals_its_clock_and_unknown_is_the_mean():
    from dlc.fald.paneltime import PanelDriveState
    law = PanelTimeLaw(closure=0.75)
    seq = [np.array([[0.2]])] * 3 + [np.array([[1.0]])] * 6
    states = {p: PanelDriveState(law, p) for p in (0, 1, None)}
    for d in seq:
        f = {p: s.fields(d) for p, s in states.items()}
        np.testing.assert_allclose(f[None][0], (f[0][0] + f[1][0]) / 2)
        np.testing.assert_allclose(f[None][1], (f[0][1] + f[1][1]) / 2)
        for s in states.values():
            s.commit(d)
    c = PanelClock(law, 1); s = PanelDriveState(law, 1)
    for d in seq:
        got = s.fields(d); s.commit(d); want = c.step(d)
        np.testing.assert_allclose(got[0], want[0]); np.testing.assert_allclose(got[1], want[1])


# ------------------------------------------------------------------------------------------ k refreshes per frame (C13)
@pytest.mark.parametrize("parity", [0, 1])
def test_refreshes_k_is_k_single_steps_with_the_same_drives(parity):
    rng = np.random.default_rng(5)
    a, b = PanelClock(PanelTimeLaw(), parity), PanelClock(PanelTimeLaw(), parity)
    for k in (1, 3, 2, 1, 5, 4, 1, 2):
        d = rng.random((3, 4))
        got = a.step(d, refreshes=k)
        want = [b.step(d) for _ in range(k)][0]                      # the pair of the frame's FIRST refresh
        np.testing.assert_array_equal(got[0], want[0]); np.testing.assert_array_equal(got[1], want[1])
        assert a.n == b.n
        np.testing.assert_array_equal(a.peek()[0], b.peek()[0]); np.testing.assert_array_equal(a.peek()[1], b.peek()[1])
    with pytest.raises(ValueError):
        a.step(np.zeros((3, 4)), refreshes=-1)


@pytest.mark.parametrize("parity", [0, 1])
def test_a_frame_replaced_inside_its_refresh_never_reaches_the_panel(parity):
    """refreshes=0 (the render loop ran twice inside one refresh of this monitor): the announced pair comes back, nothing
    is recorded — the sequence with such frames equals the sequence without them."""
    rng = np.random.default_rng(9)
    a, b = PanelClock(PanelTimeLaw(), parity), PanelClock(PanelTimeLaw(), parity)
    ghost = rng.random((2, 2))
    got = a.step(ghost, refreshes=0)                                  # before the first frame: no state, nothing kept
    np.testing.assert_array_equal(got[0], ghost); assert a.peek() is None and a.n == 0
    for k in (1, 2, 1, 3):
        d = rng.random((2, 2))
        a.step(d, refreshes=k); b.step(d, refreshes=k)
        pk = a.peek()
        got = a.step(rng.random((2, 2)), refreshes=0)
        np.testing.assert_array_equal(got[0], pk[0]); np.testing.assert_array_equal(got[1], pk[1])
        assert a.n == b.n
        np.testing.assert_array_equal(a.peek()[0], b.peek()[0]); np.testing.assert_array_equal(a.peek()[1], b.peek()[1])


def test_refresh_grid_locks_to_the_runs_when_the_real_period_is_off_nominal():
    """tests/test_fald.cpp runs the same scenarios through FaldPanelClockStep: the truth uses a REAL period that differs
    from the model's nominal one (an unlocked grid drifts onto the rounding boundary and dithers k = 0 / 2)."""
    from dlc.fald.paneltime import LOCK_GAIN, RefreshGrid
    assert LOCK_GAIN == 0.08
    rng = np.random.default_rng(11)

    def ks(times, period):
        g = RefreshGrid(period)
        return np.array([g.step(t)[1] for t in times]), g
    for hz in (60.0, 47.952):
        T = 1000.0 / hz
        for ppm in (20, 1000, -1000):
            n = int(hz * 120)
            k, g = ks(np.arange(n) * T * (1 + ppm * 1e-6) + 3.0 + rng.uniform(-2, 2, n), T)
            assert np.all(k[1:] == 1) and g.n == n - 1 and abs(g.residual) < 0.25, (hz, ppm)
    T = 1000.0 / 60.0
    t = np.arange(3600) * T + 3.0 + rng.uniform(-2, 2, 3600); t[0] += 8.0           # the seeding run half a period late
    k, _ = ks(t, T)
    assert np.all(k[60:] == 1) and int((k[1:60] != 1).sum()) <= 3
    for start in (0, 1):                                                              # a loop twice as fast as the panel
        t = np.arange(start, 7200) * (T / 2) + 1.5 + rng.uniform(-1, 1, 7200 - start)
        k, _ = ks(t, T)
        kk = k[240:]
        assert kk.max() == 1 and np.all(kk[1:] + kk[:-1] == 1)                        # a stable 1 / 0 alternation, never 2
    g = RefreshGrid(T)
    assert g.step(100.0) == (True, 0) and g.step(105.0) == (True, 0)                  # still inside the seeding refresh
    assert g.step(117.0) == (False, 1) and g.step(124.0) == (False, 0) and g.step(166.0) == (False, 3)
    assert g.step(166.0 + 5000.0) == (False, 300) and g.runs == 0                     # a long pause: a large k, no reset
    assert g.step(5166.0 + T - 5.0)[1] == 1 and g.step(5166.0 + T - 10.0) == (False, 0)   # a little backwards: k = 0
    assert g.step(0.0) == (True, 0) and g.n == 0                                      # far backwards: start over


def test_clock_ticks_count_both_parities_for_k_1_to_5():
    from dlc.fald.paneltime import clock_ticks
    for p in (0, 1):
        for n_a in range(0, 7):
            for k in range(1, 6):
                ts = sum(1 for n in range(n_a + 1, n_a + k + 1) if (n + p) % 2 == 0)
                tp = sum(1 for n in range(n_a + 1, n_a + k) if (n + p) % 2 == 0)
                assert clock_ticks(n_a, k, p) == (ts, tp), (p, n_a, k)
    # the numbers tests/test_fald.cpp pins against FaldPanelClockTicks
    assert clock_ticks(0, 1, 0) == (0, 0) and clock_ticks(0, 1, 1) == (1, 0)
    assert clock_ticks(0, 2, 0) == (1, 0) and clock_ticks(0, 2, 1) == (1, 1)
    assert clock_ticks(3, 5, 0) == (3, 2) and clock_ticks(3, 5, 1) == (2, 2)
    # k = 1: exactly one of the two clocks ticks; an even k: both tick k / 2 times
    for n_a in range(6):
        assert clock_ticks(n_a, 1, 0)[0] + clock_ticks(n_a, 1, 1)[0] == 1
        assert clock_ticks(n_a, 4, 0)[0] == clock_ticks(n_a, 4, 1)[0] == 2


@pytest.mark.parametrize("parity", [0, 1])
def test_closed_form_blend_equals_k_single_steps(parity):
    """The shader's form of the law (spec C13): between a frame first shown at n_a and the next at n_b = n_a + k every
    tick targets the first one's drives, so S(n_b) = S + aS (d_prev − S), S(n_b − 1) = S + aP (d_prev − S)."""
    from dlc.fald.paneltime import blend_factors
    law = PanelTimeLaw(closure=0.72)
    rng = np.random.default_rng(11)
    c = PanelClock(law, parity)
    d_prev = rng.random((4, 4)); c.step(d_prev, refreshes=1)
    s, n_a = d_prev.copy(), 0                                        # the closed form's own state: S(n_a) and the index
    for k in (1, 2, 3, 4, 5, 1, 1, 2, 5, 3):
        # frame a (d_prev) has been up since n_a; it stays up k refreshes in all, then frame b arrives at n_b
        if k > 1:
            c.step(d_prev, refreshes=k - 1)
        a_s, a_p = blend_factors(n_a, k, law.closure, parity)
        want_true, want_est = s + a_s * (d_prev - s), s + a_p * (d_prev - s)
        got_true, got_est = c.peek()
        np.testing.assert_allclose(got_true, want_true, rtol=0, atol=1e-12)
        np.testing.assert_allclose(got_est, want_est, rtol=0, atol=1e-12)
        d = rng.random((4, 4))
        c.step(d)
        s, d_prev, n_a = want_true, d, n_a + k


def test_settle_refreshes_formula_and_cap():
    from dlc.fald.paneltime import PanelDriveState, settle_refreshes
    assert settle_refreshes(0.72) == 14                              # 2 * ceil(ln 0.0005 / ln 0.28) + 2 (tests/test_fald.cpp pins the same)
    assert settle_refreshes(0.5) == 24 and settle_refreshes(1.0) == 4 and settle_refreshes(0.05) == 120   # 300 uncapped
    assert settle_refreshes(7.0) == 4 and settle_refreshes(-1.0) == 120       # clamped to 0.05 .. 1
    assert PanelDriveState(PanelTimeLaw(closure=0.72)).settle_frames() == 14
    # after that many refreshes of a static frame the state is within 0.05 % of the step, whatever the parity
    for parity in (0, 1):
        c = PanelClock(PanelTimeLaw(closure=0.72), parity)
        c.step(np.array([[0.0]])); c.step(np.array([[1.0]]), refreshes=14)
        true, est = c.peek()
        assert 1.0 - float(true[0, 0]) <= 0.0005 and 1.0 - float(est[0, 0]) <= 0.0005


def test_drive_state_commit_takes_refreshes():
    from dlc.fald.paneltime import PanelDriveState
    law = PanelTimeLaw(closure=0.72)
    rng = np.random.default_rng(2)
    a, b = PanelDriveState(law, None), PanelDriveState(law, None)
    for k in [2, 3, 1, 1, 2]:
        d = rng.random((2, 2))
        fa, fb = a.fields(d), b.fields(d)
        np.testing.assert_array_equal(fa[0], fb[0]); np.testing.assert_array_equal(fa[1], fb[1])
        a.commit(d, refreshes=k)
        for _ in range(k):
            b.commit(d)


def test_abs_refresh_index_formula_freshness_and_guards():
    """Stage 3: the hook's absolute index from the host's vblank map — tests/test_fald.cpp pins the same cases on C++
    FaldAbsRefreshIndex."""
    from dlc.fald.paneltime import VBLANK_LATENCY, VBLANK_MAP_MAX_AGE_MS, abs_refresh_index as idx
    assert VBLANK_LATENCY == 1 and VBLANK_MAP_MAX_AGE_MS == 500.0
    f, c, a, p, nom = 10_000_000, 5243510, 7_000_000_000, 166666.5, 16.66667     # 100-ns ticks, a 16.66665 ms map
    assert idx(c, a, p, a + 20000, f, nom) == 5243511                  # 2 ms into the anchor's refresh: shows at the next
    t10 = a + 1666665 + 150000
    assert (idx(c, a, p, t10, f, nom), idx(c, a, p, t10, f, nom, 0), idx(c, a, p, t10, f, nom, 2)) == (5243521, 5243520, 5243522)
    assert idx(c, a, p, a + 1666664, f, nom) == 5243520 and idx(c, a, p, a + 1666665, f, nom) == 5243521   # the boundary
    assert idx(c, a, p, a - 1000, f, nom) == 5243510                   # read just before the anchor: its refresh
    assert idx(c, a, p, a + 4999999, f, nom) is not None and idx(c, a, p, a + 5000001, f, nom) is None   # 0.5 s: stale
    assert idx(c, a, p, a - 333000, f, nom) == 5243509 and idx(c, a, p, a - 334000, f, nom) is None    # > 2 periods early
    assert idx(c, a, p, a + 20000, f, 20.8542) is None and idx(c, a, p, a + 20000, f, 16.4) is None    # another mode
    assert idx(c, a, p, a + 20000, f, 16.6) == 5243511 and idx(c, a, p, a + 20000, f, 0.0) == 5243511
    for bad in (0.0, -5.0, float("nan"), float("inf"), 5000.0, 2.0e7):
        assert idx(c, a, bad, a, f) is None, bad
    assert idx(c, 0, p, 100, f) is None and idx(-1, a, p, a + 20000, f) is None and idx((1 << 62) + 1, a, p, a + 20000, f) is None
    assert idx(c, a, p, a + 20000, 0, nom) is None
    assert idx(0, a, p, a - 300000, f, nom, 0) is None and idx(0, a, p, a + 20000, f, nom, 0) == 0


def test_clock_source_step_table():
    """Stage 3: which index drives the clock — the same table as tests/test_fald.cpp (C++ FaldClockSourceStep)."""
    from dlc.fald.paneltime import (SRC_ABS, SRC_GRID, SRC_HOLD, SRC_REBASE, SRC_SWITCH_ABS, SRC_SWITCH_GRID,
                                    clock_source_step)
    rows = [
        (100, False, 1, None, SRC_GRID, 1, 101), (100, False, 0, None, SRC_GRID, 0, 100),
        (100, True, 1, None, SRC_SWITCH_GRID, 1, 101), (100, False, 1, 5000, SRC_SWITCH_ABS, 1, 5000),
        (5000, True, 1, 5001, SRC_ABS, 1, 5001), (5000, True, 2, 5001, SRC_ABS, 1, 5001),
        (5000, True, 0, 5001, SRC_ABS, 1, 5001), (5000, True, 1, 5000, SRC_ABS, 0, 5000),
        (5000, True, 3, 5003, SRC_ABS, 3, 5003), (5000, True, 299, 5301, SRC_ABS, 301, 5301),
        (5000, True, 1, 4999, SRC_HOLD, 0, 5000), (5000, True, 0, 4999, SRC_HOLD, 0, 5000),
        (5000, True, 3, 4999, SRC_REBASE, 3, 4999), (5000, True, 1, 5004, SRC_REBASE, 1, 5004),
        (5000, True, 1, 4000, SRC_REBASE, 1, 4000), (2, False, 5, 3, SRC_SWITCH_ABS, 3, 3),
    ]
    for prev, prev_abs, kg, absn, src, k, n in rows:
        assert clock_source_step(prev, prev_abs, kg, absn) == (src, k, n), (prev, prev_abs, kg, absn)


def test_absolute_index_survives_a_composition_regime_shift_where_the_grid_rerolls():
    """Stage 3's point, offline: composition runs whose offset into the refresh jumps between DWM latency regimes
    (2 -> 14 -> 8 -> 2 ms). The run-time grid re-rolls its index at the shift; the absolute index from the vblank map does
    not, and with it parity 0 / 1 tick on even / odd vblank counts."""
    from dlc.fald.paneltime import SRC_ABS, RefreshGrid, abs_refresh_index, clock_source_step
    f, P = 10_000_000, 166666.5 * (1 + 30e-6)
    v0, K = 7.0e9, 5243510
    at = lambda i: int(math.floor(v0 + i * P))
    rng = np.random.default_rng(4242)
    grid = RefreshGrid(P / 1e4)
    n = None
    prev_abs = False
    dev_grid = set()
    for i in range(1200):
        t = at(i) + int((20000, 140000, 80000, 20000)[i // 300] + rng.uniform(-5000, 5000))
        j = i - i % 8                                                     # the last publish, every 8 refreshes
        absn = abs_refresh_index(K + j, at(j) + 200, P, t, f, 16.66665)
        seeded, kg = grid.step(t / 1e4)
        dev_grid.add(grid.n - i)
        if seeded:
            n, prev_abs = absn, True
            continue
        src, k, n = clock_source_step(n, prev_abs, kg, absn)
        prev_abs = absn is not None
        assert (src, k, n) == (SRC_ABS, 1, K + i + 1), i
    assert len(dev_grid) >= 2                                             # the grid alone re-rolled


def test_effective_parity_fail_safe_matches_cpp():
    """Pinned with tests/test_fald.cpp 'the parity fail-safe' (C++ FaldPanelClockEffectiveParity)."""
    from dlc.fald.paneltime import PARITY_EPOCH_ANY, PARITY_EPOCH_NONE, effective_parity
    E, E2 = 0x1234abcd5678ef90, 0x0badc0ffee000001
    assert effective_parity(-1, PARITY_EPOCH_ANY, True, E) == -1
    assert effective_parity(0, PARITY_EPOCH_ANY, False, 0) == 0
    assert effective_parity(1, PARITY_EPOCH_ANY, False, 0) == 1
    assert effective_parity(0, E, True, E) == 0
    assert effective_parity(1, E, True, E) == 1
    assert effective_parity(0, E, True, E2) == -1
    assert effective_parity(0, E, False, E) == -1
    assert effective_parity(0, PARITY_EPOCH_NONE, True, 0) == -1
    assert effective_parity(1, PARITY_EPOCH_NONE, True, E) == -1
    assert effective_parity(7, PARITY_EPOCH_ANY, True, E) == -1
