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
