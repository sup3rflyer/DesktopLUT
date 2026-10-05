"""The stage-3 count-integrity soak's analysis (dlc.fald.vblank_soak.analyse) on synthetic logs: a steady epoch passes;
an epoch change with a new offset is fine; an offset change INSIDE an epoch is a silent slip; a reset of Windows'
counter splits the truth timeline instead of reading as our slip."""
import json

from dlc.fald.vblank_soak import analyse, load_truth, truth_segments

FREQ = 10_000_000
P = 166_666.5


def _write(run, truth_rows, status_rows):
    run.mkdir(parents=True, exist_ok=True)
    with open(run / "truth.csv", "w", encoding="ascii") as fh:
        fh.write(f"# qpcfreq={FREQ} output_left=0 output_top=0 interval_ms=1000\n")
        fh.write("qpc_now,present_count,present_refresh,sync_refresh,sync_qpc,hr\n")
        for i, (qpc, count) in enumerate(truth_rows):
            fh.write(f"{qpc + 50000},{i + 1},{count},{count},{qpc},0x00000000\n")
    with open(run / "status.jsonl", "w", encoding="utf-8") as fh:
        for r in status_rows:
            fh.write(json.dumps(r) + "\n")


def _vblank(i, base=7_000_000_000):
    return int(base + i * P)


def _status(i, epoch, offset, lat=300, published=True, base=7_000_000_000, truth_base=186_000):
    """A map anchored at true vblank i (+ the wakes' latency), its count = truth count + offset."""
    return {"qpc": _vblank(i, base) + 20000, "wall": f"t{i}", "published": published, "epoch_id": epoch,
            "count_anchor": truth_base + i + offset, "qpc_anchor": _vblank(i, base) + lat, "period_ms": P * 1000 / FREQ,
            "parity_active": False}


def test_steady_epoch_and_epoch_change_pass(tmp_path):
    truth = [(_vblank(i), 186_000 + i) for i in range(0, 6000, 60)]
    status = [_status(i, "aaaa", 5) for i in range(10, 3000, 60)]
    status += [_status(i, "bbbb", 9) for i in range(3010, 5900, 60)]     # a new epoch, a new origin: fine
    _write(tmp_path / "r", truth, status)
    rep = analyse(tmp_path / "r")
    assert rep["silent_slips"] == 0 and rep["verdict"].startswith("PASS")
    assert [e["epoch_id"] for e in rep["epochs"]] == ["aaaa", "bbbb"]
    assert rep["epochs"][0]["offsets"] == {"5": 50} and rep["epochs"][1]["offsets"] == {"9": 49}
    assert rep["epochs"][0]["phase_max"] < 0.01                                          # anchors just after a vblank


def test_offset_change_inside_an_epoch_is_a_silent_slip(tmp_path):
    truth = [(_vblank(i), 186_000 + i) for i in range(0, 6000, 60)]
    status = [_status(i, "aaaa", 5) for i in range(10, 3000, 60)]
    status += [_status(i, "aaaa", 6) for i in range(3010, 5900, 60)]                    # one refresh off, same epoch
    _write(tmp_path / "r", truth, status)
    rep = analyse(tmp_path / "r")
    assert rep["silent_slips"] == 1 and rep["verdict"].startswith("FAIL")
    assert rep["epochs"][0]["silent_slips"][0][1:] == [5, 6] or rep["epochs"][0]["silent_slips"][0][1:] == (5, 6)


def test_a_reset_of_windows_counter_splits_the_truth_not_our_count(tmp_path):
    # Windows' counter restarts from 0 at vblank 3000 (a mode set): our count carries on in the same epoch
    truth = [(_vblank(i), 186_000 + i) for i in range(0, 3000, 60)]
    truth += [(_vblank(i), i - 3000) for i in range(3000, 6000, 60)]
    status = [_status(i, "aaaa", 5) for i in range(10, 2990, 60)]
    status += [{**_status(i, "aaaa", 5), "count_anchor": 186_000 + i + 5} for i in range(3010, 5900, 60)]
    _write(tmp_path / "r", truth, status)
    tr, _ = load_truth(tmp_path / "r" / "truth.csv")
    assert len(truth_segments(tr, P)) == 2
    rep = analyse(tmp_path / "r")
    assert rep["silent_slips"] == 0 and rep["truth_segments"] == 2
    assert len(rep["epochs"]) == 2                     # the same epoch id, two truth segments: reported separately


def test_pipe_errors_and_unpublished_rows_are_counted_not_judged(tmp_path):
    truth = [(_vblank(i), 186_000 + i) for i in range(0, 3000, 60)]
    status = [_status(i, "aaaa", 5) for i in range(10, 1000, 60)]
    status += [{"qpc": _vblank(1100), "wall": "x", "error": "DesktopLutApiError: pipe gone"}]
    status += [_status(i, "aaaa", 5, published=False) for i in range(1200, 1500, 60)]
    _write(tmp_path / "r", truth, status)
    rep = analyse(tmp_path / "r")
    assert rep["pipe_errors"] == 1 and rep["unpublished"] == 5 and rep["silent_slips"] == 0
