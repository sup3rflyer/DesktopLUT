"""The measured panel time law (dlc.fald.paneltime): 30-Hz sample-and-hold LEDs, compensation one frame later."""
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


def test_refresh_index_from_absolute_time_has_no_drift_and_allows_k_0():
    from dlc.fald.paneltime import refresh_index
    assert [refresh_index(t, 16.667) for t in (0.0, 8.0, 8.4, 16.7, 50.0, 1100.0, -5.0)] == [0, 0, 1, 1, 3, 66, 0]
    assert refresh_index(41.7, 20.833) == 2 and refresh_index(100.0, 0.0) == 0
    n = [refresh_index(8.3335 * i + 1.0, 16.667) for i in range(1, 201)]        # runs every half period (tests/test_fald.cpp)
    ks = np.diff([0] + n)
    assert n[-1] == 100 and sorted(set(ks)) == [0, 1] and int((ks == 0).sum()) == 100
    rng = np.random.default_rng(4)                                                # 10 000 jittered frames at 47.952 Hz
    period = 1000.0 / 47.952
    t = np.arange(1, 10001) * period + rng.uniform(-6.0, 6.0, 10000)
    assert [refresh_index(v, period) for v in t] == list(range(1, 10001))


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
