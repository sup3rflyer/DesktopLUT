"""Thermal-state alignment (dlc.thermal_align): a stage measured across a linear balance drift
is rewritten to ONE reference state; evidence/thresholds; idempotent apply + restore."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dlc import thermal_align as ta
from dlc.colormath import matvec, rgb_to_xyz_matrix

PRIM = {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06}
WHITE = (0.3127, 0.3290)
T0 = datetime(2026, 9, 3, 18, 0, 0)


def _gain(minutes: float, span: float = 0.03) -> list[float]:
    """The panel's per-channel thermal gain at ``minutes`` — red rises, blue falls, linearly."""
    f = minutes / 20.0
    return [1.0 + span * f, 1.0, 1.0 - span * f]


def _xyz(P, signal, minutes, nits=100.0, span=0.03):
    lin = [(s ** 2.2) * g for s, g in zip(signal, _gain(minutes, span))]
    return [v * nits for v in matvec(P, lin)]


def _write_stage(tmp_path: Path, *, span: float = 0.03, n_meas: int = 30, ref_every_s: float = 40.0,
                 minutes: float = 20.0) -> tuple[Path, Path]:
    P = rgb_to_xyz_matrix(*[PRIM[k] for k in ("rx", "ry", "gx", "gy", "bx", "by")], *WHITE)
    rows = []
    seq = 0
    # reference reads every ref_every_s, measurements in between
    t = 0.0
    signals = [((i % 5) / 4.0, ((i * 3) % 7) / 6.0, ((i * 5) % 9) / 8.0) for i in range(n_meas)]
    signals = [(max(s[0], 0.15), max(s[1], 0.15), max(s[2], 0.15)) for s in signals]
    i = 0
    while t <= minutes * 60.0:
        m = t / 60.0
        rows.append({"t": (T0 + timedelta(seconds=t)).isoformat(timespec="milliseconds"), "seq": seq,
                     "phase": "main", "role": "neutral_ref", "label": "ref", "rgb": [512, 512, 512],
                     "signal": [0.5, 0.5, 0.5], "xyz": _xyz(P, (0.5, 0.5, 0.5), m, span=span), "ok": True})
        seq += 1
        for k in range(3):
            if i >= n_meas:
                break
            tm = t + (k + 1) * ref_every_s / 4.0
            sig = signals[i]
            rows.append({"t": (T0 + timedelta(seconds=tm)).isoformat(timespec="milliseconds"), "seq": seq,
                         "phase": "main", "role": "measurement", "label": f"p{i:04d}",
                         "rgb": [int(round(s * 1023)) for s in sig], "signal": [round(s, 6) for s in sig],
                         "xyz": _xyz(P, sig, tm / 60.0, span=span), "ok": True, "accepted": True})
            seq += 1
            i += 1
        t += ref_every_s
    nd = tmp_path / "raw.ndjson"
    nd.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    meas = [r for r in rows if r["role"] == "measurement"]
    ti3 = tmp_path / "raw.ti3"
    body = "\n".join(
        f"{k + 1} {r['signal'][0] * 100:.4f} {r['signal'][1] * 100:.4f} {r['signal'][2] * 100:.4f} "
        f"{r['xyz'][0]:.6f} {r['xyz'][1]:.6f} {r['xyz'][2]:.6f}" for k, r in enumerate(meas))
    ti3.write_text(
        "CTI3\nDESCRIPTOR \"synthetic\"\nBEGIN_DATA_FORMAT\nSAMPLE_ID RGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z\n"
        f"END_DATA_FORMAT\nNUMBER_OF_SETS {len(meas)}\nBEGIN_DATA\n{body}\nEND_DATA\n", encoding="utf-8")
    return nd, ti3


def _xy(xyz):
    s = sum(xyz)
    return xyz[0] / s, xyz[1] / s


def test_evaluate_sees_the_drift_and_ranks_the_options(tmp_path: Path):
    nd, ti3 = _write_stage(tmp_path)
    ev = ta.evaluate(nd, ti3, PRIM, WHITE)
    assert ev["available"] is True
    assert ev["significant"] is True
    tr = ev["track"]
    assert tr["reference_rgb"] == [512, 512, 512] and tr["n"] >= 20
    assert tr["drift_x"] > 0.003            # red-up / blue-down over 20 min
    assert tr["noise_x"] < 1e-6             # a noiseless synthetic track (the trend is not noise)
    assert ev["threshold_x"] == pytest.approx(ta.DEFAULT_MIN_SPAN_X)
    assert set(ev["options"]) == {"end", "start", "mid"}
    # every measurement row matches a read; aligning to the end moves the early reads most
    assert ev["options"]["end"]["rows_matched"] == 30 and ev["options"]["end"]["rows_untouched"] == 0
    assert ev["options"]["mid"]["dx_max"] < ev["options"]["end"]["dx_max"]
    assert ev["recommendation"] == "end"
    assert "EXCEEDS" in ev["basis"]


def test_apply_end_rewrites_every_read_to_the_end_state_chromaticity(tmp_path: Path):
    nd, ti3 = _write_stage(tmp_path)
    original = ti3.read_text(encoding="utf-8")
    note = ta.apply(ti3, nd, PRIM, WHITE, "end", decided_by="test")
    assert note["align"] == "end" and note["rows_corrected"] == 30
    assert (tmp_path / "raw.ti3.orig").read_text(encoding="utf-8") == original
    assert json.loads((tmp_path / "raw_thermal_align.json").read_text())["rows_corrected"] == 30
    # each corrected row's chromaticity == what the SAME signal reads at the END state
    P = rgb_to_xyz_matrix(*[PRIM[k] for k in ("rx", "ry", "gx", "gy", "bx", "by")], *WHITE)
    data = ti3.read_text(encoding="utf-8").split("BEGIN_DATA\n")[1].split("END_DATA")[0].strip().splitlines()
    for ln in data:
        parts = ln.split()
        sig = tuple(float(v) / 100.0 for v in parts[1:4])
        got = [float(v) for v in parts[4:7]]
        want = _xyz(P, sig, 20.0)
        assert _xy(got) == pytest.approx(_xy(want), abs=2e-5)
        assert got[1] == pytest.approx(want[1], rel=0.02)   # luminance to a scalar (≤ the drift span)


def test_apply_is_idempotent_and_none_restores_the_original(tmp_path: Path):
    nd, ti3 = _write_stage(tmp_path)
    original = ti3.read_text(encoding="utf-8")
    first = ta.apply(ti3, nd, PRIM, WHITE, "end")
    aligned = ti3.read_text(encoding="utf-8")
    again = ta.apply(ti3, nd, PRIM, WHITE, "end")
    assert again.get("idempotent_replay") is True and again["rows_corrected"] == first["rows_corrected"]
    assert ti3.read_text(encoding="utf-8") == aligned
    # a changed choice starts from the ORIGINAL (never compounds)
    mid = ta.apply(ti3, nd, PRIM, WHITE, "mid")
    assert mid["align"] == "mid" and mid["dx_max"] < first["dx_max"]
    # and 'none' puts the original back
    restored = ta.apply(ti3, nd, PRIM, WHITE, "none", decided_by="seam")
    assert restored["restored_original"] is True
    assert ti3.read_text(encoding="utf-8") == original
    assert ta.note_for(ti3)["align"] == "none"


def test_flat_track_is_evidence_only(tmp_path: Path):
    nd, ti3 = _write_stage(tmp_path, span=0.0)
    ev = ta.evaluate(nd, ti3, PRIM, WHITE)
    assert ev["available"] is True and ev["significant"] is False
    assert ev["recommendation"] is None
    assert ev["options"]["end"]["dx_max"] < 1e-6


def test_missing_track_or_files_never_raise(tmp_path: Path):
    ev = ta.evaluate(tmp_path / "nope.ndjson", tmp_path / "nope.ti3", PRIM, WHITE)
    assert ev["available"] is False and "missing" in ev["reason"]
    nd = tmp_path / "two.ndjson"
    nd.write_text(json.dumps({"t": T0.isoformat(), "role": "neutral_ref", "rgb": [1, 1, 1],
                              "xyz": [1, 1, 1]}) + "\n", encoding="utf-8")
    ti3 = tmp_path / "two.ti3"
    ti3.write_text("BEGIN_DATA_FORMAT\nRGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z\nEND_DATA_FORMAT\nBEGIN_DATA\nEND_DATA\n")
    ev = ta.evaluate(nd, ti3, PRIM, WHITE)
    assert ev["available"] is False and "reference" in ev["reason"]
    with pytest.raises(ValueError):
        ta.apply(ti3, nd, PRIM, WHITE, "end")


def test_reads_below_the_noise_floor_are_left_alone(tmp_path: Path):
    nd, ti3 = _write_stage(tmp_path)
    ev = ta.evaluate(nd, ti3, PRIM, WHITE, min_nits=1e9)
    assert ev["options"]["end"]["reads"] == 0 and ev["options"]["end"]["rows_untouched"] == 30


def test_reference_track_picks_the_most_read_reference_patch():
    P = rgb_to_xyz_matrix(*[PRIM[k] for k in ("rx", "ry", "gx", "gy", "bx", "by")], *WHITE)
    from dlc.colormath import invert3x3
    reads = []
    for k in range(6):
        reads.append({"ts": float(k), "role": "neutral_ref", "rgb": [512, 512, 532],
                      "xyz": _xyz(P, (0.5, 0.5, 0.52), 0.0)})
    reads.append({"ts": 9.0, "role": "warmup", "rgb": [700, 700, 700], "xyz": _xyz(P, (0.7, 0.7, 0.7), 0.0)})
    tr = ta.reference_track(reads, invert3x3(P))
    assert tr is not None and tr.rgb == [512, 512, 532] and len(tr.rows) == 6
    assert tr.targets()["end"] == pytest.approx(tr.targets()["start"])


def test_a_remeasure_into_the_same_path_discards_the_stale_backup_and_note(tmp_path: Path):
    """The stage files live at a FIXED path per role; a re-measure overwrites them in place and
    leaves the old .orig/note behind. Those must never be replayed as 'the original' / 'already
    aligned' (adversarial review, 2026-09-03)."""
    nd, ti3 = _write_stage(tmp_path, span=0.01)
    first = ta.apply(ti3, nd, PRIM, WHITE, "end")
    assert ta.backup_state(ti3) == "aligned"
    # re-measure: a materially different dataset lands at the same paths
    nd2, ti3_2 = _write_stage(tmp_path, span=0.05)
    assert nd2 == nd and ti3_2 == ti3
    assert ta.backup_state(ti3) == "stale"
    # evidence is computed from the CURRENT file, not the stale backup
    ev = ta.evaluate(nd, ti3, PRIM, WHITE)
    assert ev["track"]["span_x"] > 3 * first["span_x"]
    assert not (tmp_path / "raw.ti3.orig").exists()          # stale backup discarded
    # and apply aligns the new data fresh (no idempotent replay of the old note)
    second = ta.apply(ti3, nd, PRIM, WHITE, "end")
    assert not second.get("idempotent_replay")
    assert second["span_x"] == ev["track"]["span_x"] and second["rows_corrected"] == 30
    assert ta.backup_state(ti3) == "aligned"
    # the real replay still short-circuits
    assert ta.apply(ti3, nd, PRIM, WHITE, "end").get("idempotent_replay") is True
    # and after 'none' the file is the original again
    ta.apply(ti3, nd, PRIM, WHITE, "none")
    assert ta.backup_state(ti3) == "original"

