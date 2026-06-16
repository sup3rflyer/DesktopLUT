"""Tests for the live readout (``dlc.readout``) — the mission-control consumer of
the ``measurements.ndjson`` stream.

Pure stdlib. Two angles: (1) the tailer / state / renderers in isolation on
hand-written records, and (2) an integration cross-check that the readout's
independently-accumulated state agrees with the measurement loop's own digest when
both consume the same stream (the human view and the LLM view must not disagree).
"""

from __future__ import annotations

import json
from pathlib import Path

from dlc.engine.patches import Transfer
from dlc.measure_loop import MeasureLoopConfig, SyntheticPanel, run_measure_loop
from dlc.readout import (
    NdjsonTailer,
    ReadoutState,
    iter_records,
    render_console_line,
    render_html,
    render_summary,
)


def _write(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec) + "\n")


def _append(path: Path, records: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec) + "\n")


def _rec(**kw) -> dict:
    base = {
        "t": "2026-06-16T00:00:00.000", "seq": 0, "phase": "main", "role": "measurement",
        "label": "p0000", "rgb": [128, 128, 128], "signal": [0.5, 0.5, 0.5], "read_index": 0,
        "xyz": [30.0, 31.0, 34.0], "yxy": [31.0, 0.31, 0.33], "nits": 31.0, "ok": True,
        "accepted": True, "agreement_de": None, "drift": None, "settle": None,
        "disposition": None, "note": None,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# tailer
# ---------------------------------------------------------------------------

def test_tailer_reads_only_new_complete_lines(tmp_path: Path):
    p = tmp_path / "m.ndjson"
    _write(p, [_rec(seq=0), _rec(seq=1)])
    t = NdjsonTailer(p)
    assert [r["seq"] for r in t.poll()] == [0, 1]
    assert t.poll() == []  # nothing new
    _append(p, [_rec(seq=2)])
    assert [r["seq"] for r in t.poll()] == [2]


def test_tailer_ignores_partial_trailing_line(tmp_path: Path):
    p = tmp_path / "m.ndjson"
    _write(p, [_rec(seq=0)])
    with p.open("a", encoding="utf-8") as h:
        h.write('{"seq":1,"partial":')  # no newline yet
    t = NdjsonTailer(p)
    assert [r["seq"] for r in t.poll()] == [0]  # partial line not consumed
    with p.open("a", encoding="utf-8") as h:
        h.write('true}\n')  # complete it
    assert [r["seq"] for r in t.poll()] == [1]


def test_tailer_resets_on_truncation(tmp_path: Path):
    p = tmp_path / "m.ndjson"
    _write(p, [_rec(seq=0), _rec(seq=1)])
    t = NdjsonTailer(p)
    assert len(t.poll()) == 2
    _write(p, [_rec(seq=9)])  # new run truncates + rewrites
    assert [r["seq"] for r in t.poll()] == [9]


def test_iter_records_non_follow_reads_all(tmp_path: Path):
    p = tmp_path / "m.ndjson"
    _write(p, [_rec(seq=0), _rec(seq=1), _rec(seq=2)])
    assert [r["seq"] for r in iter_records(p)] == [0, 1, 2]


# ---------------------------------------------------------------------------
# state accumulation
# ---------------------------------------------------------------------------

def test_state_accumulates_progress_drift_and_remeasures():
    state = ReadoutState()
    records = [
        _rec(role="warmup", label="warmup", phase="warmup", settle={"warm": False, "consecutive": 1}),
        _rec(role="warmup", label="warmup", phase="warmup", settle={"warm": True, "consecutive": 3}),
        _rec(label="p0000", xyz=[10.0, 11.0, 12.0]),
        _rec(label="p0000", read_index=1, disposition="immediate", xyz=[10.0, 11.0, 12.0]),
        _rec(label="p0001", xyz=[110.0, 120.0, 130.0]),
        _rec(role="neutral_ref", label="warmup",
             drift={"max_delta": 0.006, "repeat": True, "coldest": "B"}, accepted=False),
        _rec(label="p0001", phase="remeasure", disposition="appended", xyz=[109.0, 119.0, 129.0]),
    ]
    for r in records:
        state.update(r)
    assert state.warm is True
    assert state.warmup_reads == 2
    assert state.patches == 2          # p0000, p0001 distinct
    assert state.white_nits == 120.0   # brightest Y across all reads (p0001 first read)
    assert state.drift_episodes == 1
    assert state.cold_channel == "B"   # read from the stream, not assumed
    assert state.immediate == 1
    assert state.appended == 1
    assert state.total_reads == len(records)


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------

def test_render_console_lines_are_ascii_and_informative():
    state = ReadoutState()
    warm = _rec(role="warmup", label="warmup", phase="warmup", settle={"warm": False, "consecutive": 2})
    meas = _rec(label="p0003", xyz=[50.0, 52.0, 57.0])
    drift = _rec(role="neutral_ref", drift={"max_delta": 0.0061, "repeat": True, "coldest": "B"})
    for rec, needle in [(warm, "warm"), (meas, "p0003"), (drift, "DRIFT")]:
        state.update(rec)
        line = render_console_line(rec, state)
        assert needle in line
        assert line.isascii()             # safe in the Windows console
    drift_line = render_console_line(drift, state)
    assert "cold=B" in drift_line          # generic drift framing
    assert "0.0061" in drift_line


def test_render_summary_reports_brightness_and_drift():
    state = ReadoutState()
    for r in [_rec(label="p0", xyz=[140.0, 150.0, 163.0]),
              _rec(role="neutral_ref", drift={"max_delta": 0.005, "repeat": True, "coldest": "B"})]:
        state.update(r)
    out = render_summary(state)
    assert "brightness (white): 150.0 nits" in out
    assert "drift episodes    : 1" in out


def test_render_html_has_summary_and_rows():
    records = [
        _rec(label="p0000", xyz=[10.0, 11.0, 12.0]),
        _rec(label="p0001", xyz=[140.0, 150.0, 163.0]),
        _rec(role="neutral_ref", drift={"max_delta": 0.006, "repeat": True, "coldest": "B"}),
    ]
    html = render_html(records, live=True)
    assert "<!doctype html>" in html
    assert "p0001" in html and "drift check" in html
    assert "150.0 nits" in html
    assert "http-equiv='refresh'" in html  # live mode adds auto-refresh


# ---------------------------------------------------------------------------
# integration: the readout state must agree with the loop's own digest
# ---------------------------------------------------------------------------

def test_readout_state_agrees_with_loop_digest(tmp_path: Path):
    t = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    panel = SyntheticPanel(transfer=t, warm_tau=0.06, cold_blue_gain=0.88)
    levels = [round(i * t.max_cv / 23) for i in range(24)]
    patches = [(v, v, v) for v in levels]
    ndjson = tmp_path / "m.ndjson"
    result = run_measure_loop(
        patches=patches, transfer=t, measure=panel,
        config=MeasureLoopConfig(neutral_interval=6),
        ndjson_path=ndjson,
    )

    state = ReadoutState()
    for rec in iter_records(ndjson):
        state.update(rec)

    d = result.digest
    assert state.total_reads == d["total_reads"]
    assert state.patches == d["patch_count"]
    assert state.drift_episodes == d["drift_episodes"]
    assert state.immediate == d["immediate_remeasures"]
    assert state.appended == d["appended_remeasures"]
    assert state.warm == d["warm"]
    # the digest rounds white nits to 3 dp; the readout keeps full precision.
    assert round(state.white_nits, 3) == d["white_nits"]
