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
    assert s.corrupt is True                 # present-but-unparseable is surfaced, not hidden
    # and it can still be written over cleanly
    s.record(_rec())
    reloaded = CorrectionStore.load(path)
    assert reloaded.get("Panel One") is not None
    assert reloaded.corrupt is False         # a clean parse clears the flag


def test_absent_file_is_not_corrupt(tmp_path: Path):
    # A clean first run (no file yet) must NOT be flagged corrupt — that distinction is
    # exactly what lets a caller tell "stale/missing" from "damaged".
    s = CorrectionStore.load(tmp_path / "correction_store.json")
    assert s.get("Panel One") is None and s.corrupt is False


def test_save_is_atomic_no_partial_file(tmp_path: Path, monkeypatch):
    # If the write fails mid-flight, the PRIOR good store must remain intact (not truncated),
    # and no stray temp file is left behind — so the next run never silently loses the CCMX.
    import dlc.paths as paths

    path = tmp_path / "correction_store.json"
    good = CorrectionStore.load(path)
    good.record(_rec(correction_file="good.ccmx"))

    boom = CorrectionStore.load(path)
    boom.record(_rec(correction_file="new.ccmx"), save=False)

    real_replace = paths.os.replace
    monkeypatch.setattr(paths.os, "replace",
                        lambda src, dst: (_ for _ in ()).throw(OSError("crash during replace")))
    try:
        boom.save()
        raised = False
    except OSError:
        raised = True
    monkeypatch.setattr(paths.os, "replace", real_replace)

    assert raised
    # the original file is untouched and still parses to the prior record
    survivor = CorrectionStore.load(path)
    assert survivor.corrupt is False
    assert survivor.get("Panel One").correction_file == "good.ccmx"
    # no leftover temp files in the directory
    assert [p.name for p in tmp_path.iterdir()] == ["correction_store.json"]


def test_record_without_save_is_in_memory_only(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    store = CorrectionStore.load(path)
    store.record(_rec(), save=False)
    assert store.get("Panel One") is not None     # visible in memory
    assert not path.exists()                       # but not persisted


def test_malformed_record_is_dropped_visibly_not_silently(tmp_path):
    # Mirrors DipStore.dropped (fable audit F3-5): a hand-edited/drifted record is
    # dropped tolerantly but the loss is surfaced, never silent.
    import json
    p = tmp_path / "correction_store.json"
    good = {"display": "Panel One", "correction_file": "a.ccmx"}
    bad = {"display": "Panel Two", "strength": "not-a-number"}
    p.write_text(json.dumps({"displays": {"Panel One": good, "Panel Two": bad}}),
                 encoding="utf-8")
    store = CorrectionStore.load(p)
    assert store.get("Panel One") is not None
    assert store.get("Panel Two") is None
    assert store.dropped == ["Panel Two"]
    assert store.corrupt is False


def test_save_stamps_a_schema_version(tmp_path):
    import json
    store = CorrectionStore(tmp_path / "correction_store.json")
    store.record(CorrectionRecord(display="Panel One", correction_file="a.ccmx"))
    payload = json.loads((tmp_path / "correction_store.json").read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    assert CorrectionStore.load(tmp_path / "correction_store.json").get("Panel One") is not None
