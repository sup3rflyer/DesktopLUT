"""Recurrent panel states on the drift reference (dlc.reference_states + the measure loop).

The BenQ PD2700U (2026-09-26) mid-grey reference hops between discrete states it keeps returning
to; the loop used to treat every hop as drift (re-warm + re-measure + tighten + dense-drift retry).
A trip back into a state the panel already SETTLED in (a frozen anchor) is state, not drift. A drift
never lands back within ``near`` of a frozen anchor it has not settled in, so it keeps behaving
exactly as before — including the attacks the 2026-09-27 design review built (toggle + common-mode
drift, cool-back along the warm-in trail, meter noise at the radii).
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from dlc.drift import evaluate_drift
from dlc.engine.patches import Transfer
from dlc.measure_loop import (IncrementalMeasureSession, MeasureLoopConfig, MeasurePatch, Reading,
                              _Loop, _NdjsonWriter, run_measure_loop)
from dlc.readout import ReadoutState, render_console_line
from dlc.reference_states import (
    PERCEPTIBLE_JND,
    ReferenceStates,
    balance_delta,
    reference_shift_impact,
)


def _xyz(x: float, y: float, big_y: float) -> tuple[float, float, float]:
    return (x / y * big_y, big_y, (1.0 - x - y) / y * big_y)


# The BenQ verify-stage reference states (measured, 2026-09-27) and the stage white.
STATE_A = _xyz(0.30697, 0.31855, 23.546)
STATE_B = _xyz(0.30641, 0.31883, 23.574)
WHITE = (102.07, 107.42, 117.25)
STIM = (128, 128, 133)


def test_balance_delta_is_the_drift_trip_measure():
    ev = evaluate_drift(stabilized_xyz=STATE_A, current_xyz=STATE_B, delta_threshold=0.006)
    assert balance_delta(STATE_A, STATE_B) == pytest.approx(ev.max_channel_delta)
    assert ev.repeat   # the BenQ toggle trips the run's effective 0.006 threshold


def test_radii_need_a_noise_gap():
    with pytest.raises(ValueError):
        ReferenceStates(near=0.003, far=0.005)        # far < 2·near: noise could bridge the gap
    with pytest.raises(ValueError):
        ReferenceStates(near=0.0, far=0.006)
    ReferenceStates(near=0.003, far=0.006)


def test_toggle_is_recurrent_once_both_states_settled():
    st = ReferenceStates(near=0.003, far=0.006)
    st.settle(STATE_A, stimulus=STIM)
    # First excursion to B: never settled there — not recurrent (an episode; its re-warm may settle B).
    assert st.recurrence(STATE_B, stimulus=STIM) is None
    st.settle(STATE_B, stimulus=STIM)
    assert st.recurrence(STATE_A, stimulus=STIM)["anchor"] == 0
    assert st.recurrence(STATE_B, stimulus=STIM)["anchor"] == 1
    # A re-settle within `near` confirms the anchor; it does not found a new one or move it.
    st.settle(_xyz(0.30698, 0.31856, 23.55), stimulus=STIM)
    assert len(st.anchors) == 2 and st.anchors[0].settles == 2 and st.anchors[0].xyz == STATE_A


def test_monotonic_drift_is_never_recurrent():
    # 0.01 x over 20 min of checkpoints (the handoff's synthetic negative control): every episode
    # re-settles on the new state, and the drift never lands back on an older anchor.
    st = ReferenceStates(near=0.003, far=0.006)
    ref = STATE_A
    st.settle(ref, stimulus=STIM)
    trips = recurrent = 0
    for k in range(1, 41):
        cur = _xyz(0.30697 + 0.01 * k / 40, 0.31855, 23.546)
        if balance_delta(ref, cur) > 0.006:
            trips += 1
            if st.recurrence(cur, stimulus=STIM) is not None:
                recurrent += 1
            else:
                ref = cur
                st.settle(cur, stimulus=STIM)
        st.observe(cur, stimulus=STIM)
    assert trips >= 3 and recurrent == 0


def test_a_drifting_state_stops_matching_its_frozen_anchor():
    # Review finding 1 (chained matching): a toggle whose BOTH states slowly drift together. Matching
    # the frozen anchor (not the latest matched read) bounds what a recurrence can mask to `near`.
    st = ReferenceStates(near=0.003, far=0.006)
    st.settle(STATE_A, stimulus=STIM)
    st.settle(STATE_B, stimulus=STIM)
    matched = []
    for k in range(0, 40):
        drift = -0.0001 * k                              # common-mode x creep, away from A
        b_now = _xyz(0.30641 + drift, 0.31883, 23.574)
        m = st.recurrence(b_now, stimulus=STIM)
        matched.append(m is not None)
        if m is not None:
            # never masks beyond `near` of the anchor it matched — no chaining along the creep
            assert balance_delta(b_now, st.anchors[m["anchor"]].xyz) <= 0.003
    assert matched[0] and not matched[-1]


def test_cool_back_along_the_warm_in_trail_is_not_recurrent():
    # Review finding 2: the warm-up's approach trail is never an anchor — only the settle is.
    st = ReferenceStates(near=0.003, far=0.006)
    trail = [_xyz(0.30697 - 0.0008 * k, 0.31855, 23.5) for k in range(8, 0, -1)]   # cold → warm
    st.settle(STATE_A, stimulus=STIM)                     # only the settled end of the warm-in
    for back in trail:                                    # the panel cools back the way it came
        if balance_delta(STATE_A, back) > 0.006:
            assert st.recurrence(back, stimulus=STIM) is None


def test_noise_at_the_radii_rarely_matches_a_genuine_trip():
    # Review finding 4: with far = 2·near, a read just past `far` from the reference sits ≥ far − near
    # = near from that anchor; a match needs ANOTHER settled anchor within near. With only the
    # reference's anchor settled, a noisy genuine trip can never match.
    rng = random.Random(7)
    st = ReferenceStates(near=0.003, far=0.006)
    st.settle(STATE_A, stimulus=STIM)
    hits = 0
    for _ in range(500):
        cur = _xyz(0.30697 + 0.0009 + rng.gauss(0, 0.0001), 0.31855 + rng.gauss(0, 0.0001), 23.5)
        if balance_delta(STATE_A, cur) > 0.006 and st.recurrence(cur, stimulus=STIM) is not None:
            hits += 1
    assert hits == 0


def test_other_stimulus_never_matches():
    st = ReferenceStates(near=0.003, far=0.006)
    st.settle(STATE_B, stimulus=(128, 128, 128))     # the pre-cold-channel grey
    assert st.recurrence(STATE_B, stimulus=STIM) is None


def test_impact_measured_and_carried_to_white():
    imp = reference_shift_impact(STATE_A, STATE_B, white=WHITE, hdr=False)
    # Measured at the reference ≈ 0.35 CIEDE2000 (the recorded run's figure); carried to white by
    # the channel-gain model it grows (a*/b* scale with L*) — an upper bound, reported as such.
    assert 0.25 < imp["at_reference"] < 0.5
    assert imp["at_white"] > imp["at_reference"]
    assert imp["impact"] == pytest.approx(max(imp["at_reference"], imp["at_white"]))
    hdr = reference_shift_impact(STATE_A, STATE_B, white=None, hdr=True)
    assert hdr["impact"] > 0


def test_summary_describes_settled_states_and_their_spread():
    drift = ReferenceStates(near=0.003, far=0.006)
    drift.settle(STATE_A, stimulus=STIM)
    for k in range(10):
        drift.observe(_xyz(0.30697 + 0.001 * k, 0.31855, 23.5), stimulus=STIM)
    s = drift.summary(white=WHITE, hdr=False)
    assert s["levels"] == [] and s["impact_vs_mean"] is None and s["perceptible"] is False

    tog = ReferenceStates(near=0.003, far=0.006)
    tog.settle(STATE_A, stimulus=STIM)
    tog.settle(STATE_B, stimulus=STIM)
    for state in (STATE_A, STATE_A, STATE_B, STATE_A, STATE_B, STATE_B, STATE_A):
        tog.observe(state, stimulus=STIM)
    tog.note_recurrence(tog.recurrence(STATE_A, stimulus=STIM), trip_delta=0.0091,
                        reference=STATE_B, stimulus=STIM)
    s = tog.summary(white=WHITE, hdr=False)
    assert len(s["levels"]) == 2 and s["flips"] == 4 and s["max_recurrent_delta"] == 0.0091
    assert sum(lv["dwell"] for lv in s["levels"]) == pytest.approx(1.0)
    vm = s["impact_vs_mean"]
    assert vm["at_reference"] < s["spread"]["at_reference"]        # the mean sits between
    assert s["perceptible"] is (vm["impact"] >= PERCEPTIBLE_JND) and s["perceptible"] is False
    assert s["settled_span"]["balance_delta"] == pytest.approx(balance_delta(STATE_A, STATE_B), abs=1e-6)


# ---------------------------------------------------------------------------
# the measure loop
# ---------------------------------------------------------------------------

def _sdr8() -> Transfer:
    return Transfer.power(gamma=2.2, peak_nits=107.4, bit_depth=8)


class _TogglePanel:
    """A state-toggling LCD: the reference patch reads a state on a read-count schedule (the panel
    dwells ``dwell`` reads per state); other patches read a plain grey model. ``drift_per_read`` adds
    a monotonic x creep on top; ``gap`` scales the state separation (a perceptible toggle)."""

    def __init__(self, t: Transfer, *, dwell: int = 9, toggle: bool = True,
                 drift_per_read: float = 0.0, gap: float = 1.0) -> None:
        self.t, self.dwell, self.toggle, self.drift, self.gap = t, dwell, toggle, drift_per_read, gap
        self.n = 0

    def __call__(self, patch: MeasurePatch) -> Reading:
        self.n += 1
        in_b = self.toggle and (self.n // self.dwell) % 2 == 1
        dx = self.drift * self.n
        if patch.role == "warmup":
            x = 0.30697 + (-0.00056 * self.gap if in_b else 0.0) + dx
            y = 0.31855 + (0.00028 * self.gap if in_b else 0.0)
            Y = 23.574 if in_b else 23.546
        else:
            Y = max(0.05, self.t.cv_to_nits(max(patch.rgb)))
            x, y = 0.3127 + (-0.00056 * self.gap if in_b else 0.0) + dx, 0.3290
        xyz = _xyz(x, y, Y)
        return Reading(xyz=xyz, yxy=(Y, x, y), ok=True)


def _ramp(t: Transfer, n: int) -> list[tuple[int, int, int]]:
    return [(round(i * t.max_cv / (n - 1)),) * 3 for i in range(n)]


def _cfg(**kw) -> MeasureLoopConfig:
    base = dict(neutral_interval=4, drift_threshold=0.006, settle_threshold=0.003,
                preheat="never", cold_channel="B")
    base.update(kw)
    return MeasureLoopConfig(**base)


def test_toggling_panel_is_state_not_drift(tmp_path: Path):
    t = _sdr8()
    res = run_measure_loop(patches=_ramp(t, 60), transfer=t, measure=_TogglePanel(t),
                           config=_cfg(), ndjson_path=tmp_path / "m.ndjson")
    d = res.digest
    # Only visits to a not-yet-settled state are episodes; every return after it is state.
    assert res.drift_episodes <= 2
    assert d["state_recurrences"] >= 3
    assert d["neutral_interval_final"] == d["neutral_interval_initial"]
    assert d["drift_density_exceeded"] is False
    st = d["reference_states"]
    assert st["anchors"] == 2 and len(st["levels"]) == 2 and st["perceptible"] is False
    assert res.needs_adjudication is False
    # The recurrent checkpoints are on disk as evidence (not as drift) and the readout says so.
    recs = [json.loads(line) for line in (tmp_path / "m.ndjson").read_text().splitlines()]
    rec = next(r for r in recs if (r.get("drift") or {}).get("recurrent"))
    assert rec["drift"]["repeat"] is False and rec["drift"]["impact"]["impact"] > 0
    assert "known panel state" in render_console_line(rec, ReadoutState())


def test_toggle_recognition_off_reproduces_the_old_thrash(tmp_path: Path):
    t = _sdr8()
    res = run_measure_loop(patches=_ramp(t, 60), transfer=t, measure=_TogglePanel(t),
                           config=_cfg(recognize_recurrent_states=False),
                           ndjson_path=tmp_path / "m.ndjson")
    assert res.drift_episodes >= 4
    assert res.digest["state_recurrences"] == 0 and res.digest["reference_states"] is None


def test_monotonic_drift_still_fires_with_recognition_on(tmp_path: Path):
    t = _sdr8()
    res = run_measure_loop(patches=_ramp(t, 60), transfer=t,
                           measure=_TogglePanel(t, toggle=False, drift_per_read=0.00012),
                           config=_cfg(), ndjson_path=tmp_path / "m.ndjson")
    assert res.drift_episodes >= 2
    assert res.digest["state_recurrences"] == 0
    assert res.appended_remeasures >= 1


def test_toggle_plus_common_drift_is_not_masked(tmp_path: Path):
    # Review finding 1, end to end, at the slow rate the any-read version masked completely
    # (4e-6 x per read: 1 episode, 63 recurrences there): with frozen anchors the common-mode creep
    # leaves each anchor, so episodes resume and every recognised trip stays bounded.
    t = _sdr8()
    res = run_measure_loop(patches=_ramp(t, 300), transfer=t,
                           measure=_TogglePanel(t, drift_per_read=0.000004),
                           config=_cfg(), ndjson_path=tmp_path / "m.ndjson")
    st = res.digest["reference_states"]
    assert res.drift_episodes >= 3
    assert st["anchors"] >= 3                     # the drifted positions settled as new states
    assert st["max_recurrent_delta"] <= 0.006 + 0.003 + 0.0015   # ≤ far + near (+ the toggle step)


class _UpAndBack(_TogglePanel):
    """No toggle: the balance drifts up for 600 reads, then back down the same way."""

    def __call__(self, patch: MeasurePatch) -> Reading:
        self.n += 1
        dx = 1.5e-5 * max(0, self.n if self.n < 600 else 1200 - self.n)
        if patch.role == "warmup":
            x, y, Y = 0.30697 + dx, 0.31855, 23.546
        else:
            Y = max(0.05, self.t.cv_to_nits(max(patch.rgb)))
            x, y = 0.3127 + dx, 0.3290
        return Reading(xyz=_xyz(x, y, Y), yxy=(Y, x, y), ok=True)


def test_drift_that_returns_is_not_reported_as_a_toggle(tmp_path: Path):
    # Review round 2 finding 2: drift-episode anchors the panel never came back to are drift, not
    # toggle levels — they must not inflate the spread into a "perceptible toggle".
    t = _sdr8()
    res = run_measure_loop(patches=_ramp(t, 400), transfer=t, measure=_UpAndBack(t, toggle=False),
                           config=_cfg(), ndjson_path=tmp_path / "m.ndjson")
    st = res.digest["reference_states"]
    assert res.drift_episodes >= 5                   # the drift is still re-measured as before
    assert st["perceptible"] is False and st["levels"] == []
    assert st["drift_anchors"] == st["anchors"]
    assert st["settled_span"]["impact"] > 1.0        # …and its size is on the record


def test_impact_white_waits_for_a_real_white():
    t = _sdr8()
    loop = _Loop(patches=[], transfer=t, measure=_TogglePanel(t), config=_cfg(),
                 ndjson=_NdjsonWriter(None), events=None)
    loop.reference_xyz = STATE_A
    loop.white_xyz = STATE_A                          # only the mid-grey reference seen so far
    w = loop._impact_white()
    assert w[1] == pytest.approx(t.cv_to_nits(t.max_cv))
    assert w[0] / w[1] == pytest.approx(STATE_A[0] / STATE_A[1])   # at the reference's chromaticity
    loop.white_xyz = WHITE
    assert loop._impact_white() == WHITE


def test_perceptible_toggle_is_raised_for_adjudication(tmp_path: Path):
    t = _sdr8()
    res = run_measure_loop(patches=_ramp(t, 60), transfer=t, measure=_TogglePanel(t, gap=6.0),
                           config=_cfg(drift_threshold=0.02, settle_threshold=0.003),
                           ndjson_path=tmp_path / "m.ndjson")
    st = res.digest["reference_states"]
    assert res.digest["state_recurrences"] >= 1
    assert st["perceptible"] is True
    assert res.needs_adjudication is True
    assert "reference_states_perceptible" in res.digest["anomaly_reasons"]
    assert "previously SETTLED reference states" in res.question


def test_radii_that_touch_disable_recognition():
    t = _sdr8()
    loop = _Loop(patches=[], transfer=t, measure=_TogglePanel(t),
                 config=_cfg(settle_threshold=0.0035), ndjson=_NdjsonWriter(None), events=None)
    assert loop.reference_states is None           # 0.006 < 2 × 0.0035
    loop = _Loop(patches=[], transfer=t, measure=_TogglePanel(t), config=MeasureLoopConfig(),
                 ndjson=_NdjsonWriter(None), events=None)
    assert loop.reference_states is None           # the no-DIP defaults (0.003 / 0.004)


def test_one_off_new_state_is_one_episode_without_tightening():
    # The raw-stage 22:58:57 case: a single read in a state never settled in is an episode (the
    # conservative path), but ONE repeat never tightens the interval or trips density.
    t = _sdr8()
    loop = _Loop(patches=[], transfer=t, measure=lambda p: Reading(
        xyz=_xyz(0.30763, 0.32912, 26.14), yxy=(26.14, 0.30763, 0.32912), ok=True),
        config=_cfg(neutral_interval=22), ndjson=_NdjsonWriter(None), events=None)
    h = _xyz(0.30825, 0.32869, 26.163)
    warm = loop._warmup_patch()
    loop.reference_states.settle(h, stimulus=warm.rgb)
    loop.reference_xyz = h
    loop._neutral_checkpoint(warm, ["p0001"], final=True, patch_index=22)
    assert loop.drift_episodes == 1 and loop.state_recurrences == 0
    assert loop.neutral_interval_current == 22 and loop.neutral_interval_adjustments == 0
    assert loop.drift_density_exceeded is False


def test_recurrences_do_not_dilute_the_density_window():
    # Review finding 6: recognised recurrences must not occupy density-window slots, or a panel that
    # both toggles and drifts would delay the tighten / dense-drift latch.
    t = _sdr8()
    loop = _Loop(patches=[], transfer=t, measure=_TogglePanel(t),
                 config=_cfg(drift_density_window=3, drift_density_limit=3),
                 ndjson=_NdjsonWriter(None), events=None)
    loop.drift_checkpoints = [{"repeat": True, "recurrent": False, "max_delta": 0.01},
                              {"repeat": False, "recurrent": True, "max_delta": 0.009},
                              {"repeat": True, "recurrent": False, "max_delta": 0.01},
                              {"repeat": False, "recurrent": True, "max_delta": 0.009},
                              {"repeat": True, "recurrent": False, "max_delta": 0.01}]
    assert loop._recent_drift_summary()["recent_repeats"] == 3


def test_checkin_carries_the_recurrence_evidence(tmp_path: Path):
    from dlc.events import RunLog, read_events
    t = _sdr8()
    runlog = RunLog(tmp_path / "events.jsonl")
    run_measure_loop(patches=_ramp(t, 60), transfer=t, measure=_TogglePanel(t), config=_cfg(),
                     ndjson_path=tmp_path / "m.ndjson", runlog=runlog, checkin_interval_s=1e-6)
    packets = [e.data for e in read_events(tmp_path / "events.jsonl") if e.event == "check_in"]
    with_states = [p for p in packets if p.get("reference_states")]
    assert with_states, "a check-in after a recurrence must carry the state evidence"
    block = with_states[-1]["reference_states"]
    assert {"recurrences", "anchors", "max_recurrent_delta", "impact_vs_mean", "perceptible"} <= set(block)
    assert any((p.get("since_last") or {}).get("state_recurrences") for p in packets)


def test_incremental_session_stays_conservative(tmp_path: Path):
    t = _sdr8()
    sess = IncrementalMeasureSession(patches=_ramp(t, 40), transfer=t, measure=_TogglePanel(t),
                                     config=_cfg())
    sess.start()
    for i in range(40):
        sess.measure_index(i)
    d = sess.finish()
    assert "state_recurrences" in d and "reference_states" in d
    if d["state_recurrences"]:
        assert d["needs_adjudication"] is True
