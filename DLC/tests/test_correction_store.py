"""Tests for the persistent per-display correction store (``dlc.correction_store``).

The store is the corrections' cross-run "medical history" (sibling of the GS+WB
``tweak_history.json``): a JSON map ``(display, mode) -> CorrectionRecord``, upserted by
slot, tolerant of a missing or corrupt file. Pure stdlib — no engine deps.
"""

from __future__ import annotations

from pathlib import Path

import json

import pytest

from dlc.correction_store import (MODE_LEGACY_TAG, MODE_LEGACY_UNTAGGED, MODE_RECORDED,
                                  CorrectionRecord, CorrectionStore, infer_mode_from_filename)


def _rec(display="Panel One", mode="SDR", **kw) -> CorrectionRecord:
    base = dict(mode=mode, correction_file="c.ccmx", correction_made="2026-05-01", spd_file="white.sp",
                white_xy=[0.308, 0.325], white_provenance="spd_crt_like", observer="2015_2",
                anchor="reference", strength=0.5, updated="2026-06-16")
    base.update(kw)
    return CorrectionRecord(display=display, **base)


def test_empty_store_get_returns_none(tmp_path: Path):
    s = CorrectionStore.load(tmp_path / "correction_store.json")
    assert s.get("Panel One", "SDR") is None
    assert s.records() == {}


def test_record_round_trips(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    CorrectionStore.load(path).record(_rec())
    again = CorrectionStore.load(path).get("Panel One", "SDR")
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
    assert reloaded.get("Panel One", "SDR").correction_made == "2026-06-10"
    assert set(reloaded.records()) == {("Panel One", "SDR"), ("Panel Two", "SDR")}


def test_malformed_file_is_tolerated(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    path.write_text("{ not valid json", encoding="utf-8")
    s = CorrectionStore.load(path)          # must not raise
    assert s.get("Panel One", "SDR") is None
    assert s.corrupt is True                 # present-but-unparseable is surfaced, not hidden
    # and it can still be written over cleanly
    s.record(_rec())
    reloaded = CorrectionStore.load(path)
    assert reloaded.get("Panel One", "SDR") is not None
    assert reloaded.corrupt is False         # a clean parse clears the flag


def test_absent_file_is_not_corrupt(tmp_path: Path):
    # A clean first run (no file yet) must NOT be flagged corrupt — that distinction is
    # exactly what lets a caller tell "stale/missing" from "damaged".
    s = CorrectionStore.load(tmp_path / "correction_store.json")
    assert s.get("Panel One", "SDR") is None and s.corrupt is False


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
    assert survivor.get("Panel One", "SDR").correction_file == "good.ccmx"
    # no leftover temp files in the directory
    assert [p.name for p in tmp_path.iterdir()] == ["correction_store.json"]


def test_record_without_save_is_in_memory_only(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    store = CorrectionStore.load(path)
    store.record(_rec(), save=False)
    assert store.get("Panel One", "SDR") is not None     # visible in memory
    assert not path.exists()                       # but not persisted


def test_malformed_record_is_dropped_visibly_not_silently(tmp_path):
    # Mirrors DipStore.dropped (fable audit F3-5): a hand-edited/drifted record is
    # dropped tolerantly but the loss is surfaced, never silent.
    p = tmp_path / "correction_store.json"
    good = {"display": "Panel One", "correction_file": "a.ccmx"}
    bad = {"display": "Panel Two", "strength": "not-a-number"}
    p.write_text(json.dumps({"displays": {"Panel One": good, "Panel Two": bad}}),
                 encoding="utf-8")
    store = CorrectionStore.load(p)
    assert store.get("Panel One", "SDR") is not None
    assert store.get("Panel Two", "SDR") is None
    assert store.dropped == ["Panel Two"]
    assert store.corrupt is False


def test_save_stamps_a_schema_version(tmp_path):
    store = CorrectionStore(tmp_path / "correction_store.json")
    store.record(CorrectionRecord(display="Panel One", mode="SDR", correction_file="a.ccmx"))
    payload = json.loads((tmp_path / "correction_store.json").read_text(encoding="utf-8"))
    assert payload["schema"] == 2
    assert payload["displays"]["Panel One"]["SDR"]["correction_file"] == "a.ccmx"
    assert CorrectionStore.load(tmp_path / "correction_store.json").get("Panel One", "SDR") is not None


# ---------------------------------------------------------------------------
# Mode-keyed slots (schema 2). Schema 1 kept ONE record per display, so ingesting the
# HDR CCMX replaced the SDR one and months of PA32UCXR SDR runs measured through the
# HDR correction. Each mode now has its own slot and never reads the other's.
# ---------------------------------------------------------------------------

_BIN = "third_party\\argyll\\3.3.0\\bin\\"
_PA = "Asus ProArt PA32UCXR"
_PA_SDR = _BIN + "Asus_ProArt_PA32UCXR-ColorChecker-i1Display3.ccmx"
_PA_HDR = _BIN + "Asus_ProArt_PA32UCXR_HDR-ColorChecker-i1Display3.ccmx"


def _write_schema1(path: Path, records: dict) -> None:
    path.write_text(json.dumps({"schema": 1, "displays": records}), encoding="utf-8")


def test_sdr_and_hdr_slots_coexist(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    store = CorrectionStore.load(path)
    store.record(_rec(display=_PA, mode="SDR", correction_file=_PA_SDR))
    store.record(_rec(display=_PA, mode="HDR", correction_file=_PA_HDR))   # must NOT replace SDR
    again = CorrectionStore.load(path)
    assert again.get(_PA, "SDR").correction_file == _PA_SDR
    assert again.get(_PA, "HDR").correction_file == _PA_HDR
    assert set(again.modes_for(_PA)) == {"SDR", "HDR"}
    assert again.get(_PA, "sdr").correction_file == _PA_SDR     # mode is case-normalised


def test_one_mode_never_answers_for_the_other(tmp_path: Path):
    store = CorrectionStore.load(tmp_path / "correction_store.json")
    store.record(_rec(display=_PA, mode="HDR", correction_file=_PA_HDR))
    assert store.get(_PA, "SDR") is None


def test_record_requires_a_mode(tmp_path: Path):
    store = CorrectionStore.load(tmp_path / "correction_store.json")
    with pytest.raises(ValueError, match="no mode"):
        store.record(CorrectionRecord(display=_PA, correction_file=_PA_SDR))
    with pytest.raises(ValueError):
        store.get(_PA, "DolbyVision")


def test_recorded_slot_is_stamped_recorded(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    CorrectionStore.load(path).record(_rec(mode="HDR"))
    rec = CorrectionStore.load(path).get("Panel One", "HDR")
    assert rec.mode == "HDR" and rec.mode_source == MODE_RECORDED
    assert CorrectionStore.load(path).mode_inferences() == []


@pytest.mark.parametrize("name, mode", [
    (_PA_HDR, "HDR"),
    (_PA_SDR, None),                                   # the probe-match SDR name is untagged
    ("C:/cal/LG_OLED_C6_42_HDR-ColorChecker-i1Display3.ccmx", "HDR"),
    ("panel_SDR.ccss", "SDR"),
    ("panel-hdr.ccmx", "HDR"),
    ("Asus_ProArt_PA32UCXR_HDR_white.sp", "HDR"),
    ("SHDRX.ccmx", None),                              # not a delimited tag
    ("a_HDR_SDR.ccmx", None),                          # contradictory → no guess
    (None, None),
])
def test_infer_mode_from_filename(name, mode):
    assert infer_mode_from_filename(name) == mode


def test_legacy_hdr_record_loads_into_the_hdr_slot_only(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    _write_schema1(path, {_PA: {"display": _PA, "correction_file": _PA_HDR, "correction_made": "2026-06-19"}})
    store = CorrectionStore.load(path)
    assert store.get(_PA, "HDR").correction_file == _PA_HDR
    assert store.get(_PA, "SDR") is None                # NOT silently applied to SDR as well
    assert store.get(_PA, "HDR").mode_source == MODE_LEGACY_TAG
    assert store.dropped == [] and store.corrupt is False


def test_legacy_untagged_record_is_sdr_and_surfaced(tmp_path: Path):
    # The 2026-09-25 hand-edited file: SDR ccmx (untagged) + the HDR white SPD.
    path = tmp_path / "correction_store.json"
    _write_schema1(path, {_PA: {"display": _PA, "correction_file": _PA_SDR,
                                "spd_file": _BIN + "Asus_ProArt_PA32UCXR_HDR_white.sp"}})
    store = CorrectionStore.load(path)
    assert store.get(_PA, "SDR").correction_file == _PA_SDR     # the SPD tag does NOT decide
    assert store.get(_PA, "HDR") is None
    [note] = store.mode_inferences()
    assert note["mode"] == "SDR" and note["basis"] == MODE_LEGACY_UNTAGGED
    assert "tagged HDR" in note["spd_conflict"]


def test_legacy_record_without_correction_uses_the_spd_tag(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    _write_schema1(path, {_PA: {"display": _PA, "correction_file": None, "spd_file": "x_HDR_white.sp"}})
    assert CorrectionStore.load(path).get(_PA, "HDR") is not None


def test_legacy_file_rewrites_as_schema2_keeping_the_inference_visible(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    _write_schema1(path, {_PA: {"display": _PA, "correction_file": _PA_SDR}})
    CorrectionStore.load(path).save()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == 2 and set(payload["displays"][_PA]) == {"SDR"}
    # the guess is persisted as a guess until a correction is freshly recorded for the slot
    assert CorrectionStore.load(path).mode_inferences()[0]["basis"] == MODE_LEGACY_UNTAGGED


def test_migration_ends_with_both_proart_slots(tmp_path: Path):
    # The 2026-09-25 state: live file = SDR ccmx (hand-edited), backup = the HDR record.
    live, backup = tmp_path / "correction_store.json", tmp_path / "backup.json"
    _write_schema1(live, {_PA: {"display": _PA, "correction_file": _PA_SDR, "correction_made": "2026-06-16"}})
    _write_schema1(backup, {_PA: {"display": _PA, "correction_file": _PA_HDR, "correction_made": "2026-06-19"}})
    store = CorrectionStore.load(live)
    store.record(CorrectionStore.load(backup).get(_PA, "HDR"))
    again = CorrectionStore.load(live)
    assert again.get(_PA, "SDR").correction_file == _PA_SDR
    assert again.get(_PA, "HDR").correction_file == _PA_HDR
    assert again.get(_PA, "HDR").correction_made == "2026-06-19"


def test_bad_mode_slot_is_dropped_visibly(tmp_path: Path):
    path = tmp_path / "correction_store.json"
    path.write_text(json.dumps({"schema": 2, "displays": {_PA: {
        "SDR": {"correction_file": _PA_SDR},
        "XDR": {"correction_file": "x.ccmx"},
        "HDR": {"strength": "not-a-number"}}}}), encoding="utf-8")
    store = CorrectionStore.load(path)
    assert store.get(_PA, "SDR").correction_file == _PA_SDR
    assert sorted(store.dropped) == [f"{_PA}:HDR", f"{_PA}:XDR"]
