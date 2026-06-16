"""Tests for the persistent per-display correction store (``dlc.correction_store``).

The store is the corrections' cross-run "medical history" (sibling of the GS+WB
``tweak_history.json``): a JSON map ``display -> CorrectionRecord``, upserted by
display, tolerant of a missing or corrupt file. Pure stdlib — no engine deps.
"""

from __future__ import annotations

from pathlib import Path

from dlc.correction_store import CorrectionRecord, CorrectionStore


def _rec(display="Panel One", **kw) -> CorrectionRecord:
    base = dict(correction_file="c.ccmx", correction_made="2026-05-01", spd_file="white.sp",
                white_xy=[0.308, 0.325], white_provenance="spd_crt_like", observer="2015_2",
                anchor="reference", strength=0.5, updated="2026-06-16")
    base.update(kw)
    return CorrectionRecord(display=display, **base)


def test_empty_store_get_returns_none(tmp_path: Path):
    s = CorrectionStore.load(tmp_path / "correction_store.json")
    assert s.get("Panel One") is None
    assert s.records() == {}


def test_record_round_trips(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    CorrectionStore.load(path).record(_rec())
    again = CorrectionStore.load(path).get("Panel One")
    assert again is not None
    assert again.correction_made == "2026-05-01"
    assert again.white_xy == [0.308, 0.325]
    assert again.white_provenance == "spd_crt_like"
    assert again.strength == 0.5
    assert again.spd_file == "white.sp"


def test_record_upserts_by_display(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    store = CorrectionStore.load(path)
    store.record(_rec(correction_made="2026-05-01"))
    store.record(_rec(correction_made="2026-06-10"))   # same display → overwrite
    store.record(_rec(display="Panel Two"))
    reloaded = CorrectionStore.load(path)
    assert reloaded.get("Panel One").correction_made == "2026-06-10"
    assert set(reloaded.records()) == {"Panel One", "Panel Two"}


def test_malformed_file_is_tolerated(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    path.write_text("{ not valid json", encoding="utf-8")
    s = CorrectionStore.load(path)          # must not raise
    assert s.get("Panel One") is None
    # and it can still be written over cleanly
    s.record(_rec())
    assert CorrectionStore.load(path).get("Panel One") is not None


def test_record_without_save_is_in_memory_only(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    store = CorrectionStore.load(path)
    store.record(_rec(), save=False)
    assert store.get("Panel One") is not None     # visible in memory
    assert not path.exists()                       # but not persisted
