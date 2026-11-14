"""DIP knowledge (``dlc.knowledge``), stages S1 + S2: data model, validator, append-only rules, rollups, the
no-table rule, unit-id resolution, the lock + CAS write, the rendered view (golden file) and the CLI end to end.

Everything runs on temp folders; nothing touches DLC's own dip_store.json or a dip_knowledge folder beside it."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from dlc import knowledge as kn
from dlc.knowledge import (ConcurrentWrite, CorruptFile, KnowledgeError, KnowledgeStore, UnitNotFound, apply_op,
                           check_append_only, entry_state, render, unit_id_for, validate_unit)

TODAY = "2026-11-14"
GOLDEN = Path(__file__).parent / "data" / "knowledge_golden.md"
BY = "llm:test"


@pytest.fixture()
def ev(tmp_path: Path) -> Path:
    p = tmp_path / "evidence" / "dense.json"
    p.parent.mkdir(parents=True)
    p.write_bytes(b'{"tables": "raw reads live here"}\n')
    return p


def _header() -> dict:
    return {"identity": {"hardware_id": "TST1234", "edid_id": "TST1234-12345",
                         "device_paths": [r"\\?\DISPLAY#TST1234#5&1&UID1#{x}"]},
            "aliases": ["Test Panel 32"], "model": "Test Panel 32 mini-LED", "panel": {"tech": "mini-LED IPS",
                                                                                     "zones": None}}


def _measured(ev: Path, **kw) -> dict:
    e = {"kind": "panel", "title": "Full-field grey light is a sawtooth in the code",
         "statement": "Over codes 118-190 the light rises in teeth of 2-4 % every 4-12 codes; chromaticity flat.",
         "scope": {"mode": ["SDR"], "domain": "sdr_drive_code10", "code_range": [118, 190],
                   "stimulus": {"pattern": "full field", "area_pct": 100},
                   "unknown": ["window instead of full field", "HDR"]},
         "status": "measured",
         "observations": [
             {"date": "2026-11-14", "verdict": "establishes", "independent": True,
              "source": {"kind": "dense read", "path": str(ev)},
              "numbers": {"teeth_pct": [2, 4], "max_dev_pct": 4.43}},
             {"date": "2026-09-25", "verdict": "matches", "independent": True,
              "source": {"kind": "raw ramp", "ref": "an older run, file not kept"},
              "numbers": {"loo_light_structure_pct": 0.92, "ratio_to_g22": {"132": 0.88, "165": 0.874}}}],
         "signature": {"type": "code_structure", "domain": "sdr_drive_code10", "code_range": [118, 190],
                       "quantity": "light", "max_abs_dev_pct": 4.43, "period_codes": [4, 12],
                       "chroma_dxy_max": 0.001},
         "facets": {"grey_light_structure": {"mode": "SDR", "stimulus_area_pct": 100, "code_range": [118, 190],
                                             "max_abs_dev_pct": 4.43, "balance": "flat"}},
         "implications": [{"flows": ["grayscale-wb"], "text": "Correct light as a trend, balance per grey."}],
         "open_questions": [{"q": "Window or full field?", "test": "dense read in a 10 % window",
                             "owner_action": False},
                            {"q": "Local dimming off?", "test": "dimming off in the monitor menu",
                             "owner_action": True}],
         "links": [{"rel": "mechanism_hypothesis", "id": "H-0001"}]}
    e.update(kw)
    return e


def _hyp(**kw) -> dict:
    e = {"kind": "panel", "id": "H-0001", "title": "Hypothesis: the backlight steps for a full-field grey",
         "statement": "Proposed mechanism for F-0001: the local dimming sets the backlight in steps.",
         "scope": {"mode": ["SDR"], "unknown": ["everything except full field, dimming on"]},
         "status": "hypothesis", "links": [{"rel": "explains?", "id": "F-0001"}]}
    e.update(kw)
    return e


def _event(**kw) -> dict:
    e = {"kind": "event", "title": "An evening with a flat HDR peak", "explained": False,
         "statement": "One evening the full stack read a flat 1469 nit full field; not reproduced two days later.",
         "scope": {"mode": ["HDR"], "unknown": ["cause"]}, "status": "hypothesis",
         "signature": {"type": "luminance_cap", "level_nits": [1360, 1476]}}
    e.update(kw)
    return e


def _store(tmp_path: Path) -> KnowledgeStore:
    return KnowledgeStore(tmp_path / "dip_knowledge")


def _create(store: KnowledgeStore, entries: list[dict], *, today: str = TODAY) -> dict:
    hdr = kn.unit_from_header(_header())
    ops = [{"op": "create_entry", "by": BY, "why": "first entries", "entry": e} for e in entries]
    return store.apply_ops(hdr["unit_id"], ops, create=hdr, today=today)


def _uid() -> str:
    return "TST1234-12345"


# ----------------------------------------------------------------------------- identity
def test_unit_id_prefers_a_serialled_edid_id_else_hardware_id_and_device_path_hash():
    assert unit_id_for(hardware_id="AUS322A", edid_id="AUS322A-335544320") == "AUS322A-335544320"
    dp = r"\\?\DISPLAY#BNQ7F5A#5&1&UID2#{y}"
    want = "BNQ7F5A~" + hashlib.sha1(dp.lower().encode()).hexdigest()[:8]
    assert unit_id_for(hardware_id="BNQ7F5A", edid_id="BNQ7F5A-0", device_path=dp) == want
    assert unit_id_for(hardware_id="BNQ7F5A", device_path=dp) == want
    with pytest.raises(KnowledgeError):
        unit_id_for(hardware_id="BNQ7F5A", edid_id="BNQ7F5A-0")


def test_header_unit_id_must_match_its_identity():
    h = _header()
    h["unit_id"] = "SOMETHING-1"
    with pytest.raises(KnowledgeError, match="is not the id its identity gives"):
        kn.unit_from_header(h)


# ----------------------------------------------------------------------------- round trip
def test_round_trip_and_file_shape(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    doc = _create(store, [_measured(ev), _hyp(), _event()])
    raw = store.path(_uid()).read_text(encoding="utf-8")
    assert raw == json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    again = store.load(_uid())
    assert again == doc and validate_unit(again) == []
    assert [e["id"] for e in again["entries"]] == ["E-0001", "F-0001", "H-0001"]
    f1 = again["entries"][1]
    assert f1["rev"] == 1 and f1["revisions"][0]["op"] == "create_entry"
    assert f1["status_history"] == [{"status": "measured", "date": TODAY, "by": BY, "why": "first entries"}]
    assert f1["last_confirmed"] == "2026-11-14" and f1["created"] == TODAY
    o1 = f1["observations"][0]
    assert o1["id"] == "O1" and o1["by"] == BY
    assert o1["source"]["sha256"] == hashlib.sha256(ev.read_bytes()).hexdigest() and o1["source"]["hash_of"] == "file"
    assert o1["source"]["path"] == str(ev).replace("\\", "/")
    assert [q["id"] for q in f1["open_questions"]] == ["Q1", "Q2"]
    assert store.md_path(_uid()).read_text(encoding="utf-8").startswith(
        f"<!-- generated from {_uid()}.json rev 1, do not edit -->")
    assert doc["rev"] == 1 and len(doc["revisions"]) == 3


def test_ids_are_allocated_per_prefix_and_never_reused(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    _create(store, [_measured(ev), _hyp()])
    e2 = _measured(ev, title="Another panel fact", scope={"mode": ["HDR"], "unknown": ["SDR"]},
                   signature={"type": "none"}, links=[])
    doc = store.apply_ops(_uid(), [{"op": "create_entry", "by": BY, "why": "more", "entry": e2}], today=TODAY)
    assert "F-0002" in [e["id"] for e in doc["entries"]]
    with pytest.raises(KnowledgeError, match="exists"):
        store.apply_ops(_uid(), [{"op": "create_entry", "by": BY, "why": "x", "entry": {**e2, "id": "F-0002"}}])
    with pytest.raises(KnowledgeError, match="never reused"):
        store.apply_ops(_uid(), [{"op": "create_entry", "by": BY, "why": "x", "entry": {**_hyp(), "id": "H-0001"}}])


# ----------------------------------------------------------------------------- refusals (every rule names itself)
def _refused(tmp_path: Path, ev: Path, entry: dict, match: str) -> None:
    store = _store(tmp_path / f"r{abs(hash(match)) % 10 ** 8}")
    with pytest.raises(KnowledgeError) as ei:
        _create(store, [entry])
    assert match in str(ei.value), str(ei.value)
    assert not store.path(_uid()).exists()


def test_every_refusal_is_named(tmp_path: Path, ev: Path):
    m = _measured(ev, links=[])
    cases = [
        ({**m, "colour": "x"}, "keys the store sets or does not know"),
        ({**m, "kind": "monitor"}, "kind must be one of"),
        ({**m, "id": "R-0001"}, "does not fit kind panel"),
        ({**m, "status": "probable"}, "status: one of"),
        ({**m, "title": "x" * 91}, "91 characters (limit 90)"),
        ({**m, "statement": "y" * 1201}, "1201 characters (limit 1200)"),
        ({**m, "scope": {"mode": ["SDR"]}}, "scope.unknown: required"),
        ({**m, "scope": {**m["scope"], "unknown": []}}, "must name what was not tested"),
        ({**m, "scope": {**m["scope"], "gamma": "2.2"}}, "unknown key 'gamma'"),
        ({**m, "scope": {**m["scope"], "mode": ["SDR", "DV"]}}, "scope.mode"),
        ({**m, "observations": []}, "needs observations with numbers"),
        ({**m, "observations": [{**m["observations"][1]}]}, "at least one observation with a hashed evidence"),
        ({**m, "observations": [{**m["observations"][0], "numbers": {}}]}, "an observation needs numbers"),
        ({**m, "observations": [{**m["observations"][0], "verdict": "fine"}]}, "verdict: one of"),
        ({**m, "observations": [{**m["observations"][0], "independent": "yes"}]}, "independent"),
        ({**m, "observations": [{**m["observations"][0], "source": {"kind": "x", "path": str(ev) + ".gone"}}]},
         "evidence path does not exist"),
        ({**m, "observations": [{**m["observations"][0], "source": {"kind": "x", "path": str(ev), "sha256": "0" * 64}}]},
         "does not match the file now"),
        ({**m, "observations": [{**m["observations"][0], "source": {"kind": "x"}}]}, "needs a 'path'"),
        ({**m, "signature": {"type": "wiggle"}}, "is not one of"),
        ({**m, "signature": {**m["signature"], "teeth": 3}}, "unknown key 'teeth'"),
        ({**m, "signature": {"type": "code_structure", "domain": "sdr_drive_code10"}}, "code_structure needs"),
        ({**m, "signature": {**m["signature"], "quantity": "hue"}}, "quantity: one of"),
        ({**m, "facets": {"grey_teeth": {}}}, "unknown facet"),
        ({**m, "facets": {"grey_light_structure": {"mode": "SDR"}}}, "needs 'code_range'"),
        ({**m, "facets": {"link_step": {"bpc": 10, "pct": [0.9, 2.3], "corr": {"1": 2}}}},
         "facet values are numbers"),
        ({**m, "facets": {"link_step": {"bpc": 10, "pct": [0.9, 1.0, 1.2, 1.5, 2.3]}}},
         "numeric array of 5 values (limit 4)"),
        ({**m, "observations": [{**m["observations"][0], "numbers": {"reads": list(range(17))}}]},
         "numeric array of 17 values (limit 16)"),
        ({**m, "observations": [{**m["observations"][0],
                                 "numbers": {"by_code": {str(c): 1.0 for c in range(100, 117)}}}]},
         "number-keyed map of 17 entries"),
        ({**m, "implications": [{"flows": ["x"], "text": "t"}], "statement": m["statement"],
          "signature": {**m["signature"], "codes_by": {"118": 1}}}, "unknown key 'codes_by'"),
        ({**m, "links": [{"rel": "causes", "id": "F-0001"}]}, "rel: one of"),
        ({**m, "links": [{"rel": "refines", "id": "F-0009"}]}, "F-0009 is not an entry of this unit"),
        ({**m, "status": "superseded"}, "names its successor"),
        ({**m, "explained": False}, "only events carry it"),
        ({**_event(), "explained": None}, "an event says whether"),
        ({**_hyp(), "status": "inferred", "links": []}, "an inferred entry needs"),
        ({**m, "open_questions": [{"q": "?", "test": "t"}]}, "owner_action"),
        ({**m, "transferable": "yes"}, "transferable: one of"),
    ]
    for entry, match in cases:
        _refused(tmp_path, ev, entry, match)


def test_no_number_keyed_table_outside_observation_numbers(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    doc = _create(store, [_measured(ev, links=[])])
    bad = copy.deepcopy(doc)
    bad["entries"][0]["scope"]["panel_settings"] = {"118": "on", "120": "off"}
    errs = validate_unit(bad)
    assert any("number-keyed map" in e for e in errs), errs
    bad = copy.deepcopy(doc)
    bad["entries"][0]["statement_table"] = list(range(30))
    assert any("unknown key 'statement_table'" in e for e in validate_unit(bad))


def test_bad_op_by_and_why_are_refused(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    _create(store, [_measured(ev, links=[])])
    for op, match in (({"op": "delete_entry", "by": BY, "why": "x", "id": "F-0001"}, "unknown op"),
                      ({"op": "set_status", "by": "claude", "why": "x", "id": "F-0001", "status": "refuted"},
                       "'by' must be"),
                      ({"op": "set_status", "by": BY, "why": "", "id": "F-0001", "status": "refuted"},
                       "must be a non-empty string"),
                      ({"op": "set_status", "by": BY, "why": "x", "id": "F-0001", "status": "refuted", "x": 1},
                       "unknown keys ['x']"),
                      ({"op": "set_status", "by": BY, "why": "x", "id": "F-0009", "status": "refuted"},
                       "no entry 'F-0009'")):
        before = store.path(_uid()).read_bytes()
        with pytest.raises(KnowledgeError, match=match.replace("[", r"\[").replace("]", r"\]")):
            store.apply_ops(_uid(), [op])
        assert store.path(_uid()).read_bytes() == before


def test_two_measured_entries_with_one_signature_and_overlapping_scope_need_a_link(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    _create(store, [_measured(ev, links=[])])
    twin = _measured(ev, title="Teeth again, narrower", links=[],
                     scope={**_measured(ev)["scope"], "code_range": [140, 170]})
    with pytest.raises(KnowledgeError, match="one must 'refines'"):
        store.apply_ops(_uid(), [{"op": "create_entry", "by": BY, "why": "x", "entry": twin}])
    with pytest.raises(KnowledgeError, match="one must 'refines'"):     # a hypothesised link is not enough
        store.apply_ops(_uid(), [{"op": "create_entry", "by": BY, "why": "x",
                                  "entry": {**twin, "links": [{"rel": "refines?", "id": "F-0001"}]}}])
    doc = store.apply_ops(_uid(), [{"op": "create_entry", "by": BY, "why": "x",
                                    "entry": {**twin, "links": [{"rel": "refines", "id": "F-0001"}]}}])
    assert [e["id"] for e in doc["entries"]] == ["F-0001", "F-0002"]
    # disjoint ranges do not collide
    apart = _measured(ev, title="Teeth up high", links=[], scope={**_measured(ev)["scope"], "code_range": [400, 600]})
    store.apply_ops(_uid(), [{"op": "create_entry", "by": BY, "why": "x", "entry": apart}])


def test_an_event_may_share_a_signature_type_with_a_property(tmp_path: Path, ev: Path):
    # an event is a happening, not a property: no link is demanded (the S3 matcher's 'ambiguous' covers it)
    store = _store(tmp_path)
    _create(store, [_measured(ev, links=[])])
    happening = {**_event(), "status": "measured",
                 "signature": _measured(ev)["signature"],
                 "scope": {"mode": ["SDR"], "unknown": ["cause"]},
                 "observations": [_measured(ev)["observations"][0]]}
    doc = store.apply_ops(_uid(), [{"op": "create_entry", "by": BY, "why": "x", "entry": happening}], today=TODAY)
    assert [e["id"] for e in doc["entries"]] == ["E-0001", "F-0001"]


def test_hard_size_limit_and_active_entry_cap(tmp_path: Path, ev: Path, monkeypatch):
    store = _store(tmp_path)
    _create(store, [_measured(ev, links=[])])
    monkeypatch.setattr(kn, "HARD_LIMIT_BYTES", 2000)
    with pytest.raises(KnowledgeError, match="over the hard limit"):
        store.apply_ops(_uid(), [{"op": "create_entry", "by": BY, "why": "x", "entry": _hyp(id=None, links=[])}])
    monkeypatch.setattr(kn, "HARD_LIMIT_BYTES", 1024 * 1024)
    monkeypatch.setattr(kn, "MAX_ACTIVE_ENTRIES", 2)
    store.apply_ops(_uid(), [{"op": "create_entry", "by": BY, "why": "x", "entry": {**_hyp(links=[])}}])
    with pytest.raises(KnowledgeError, match="supersede or merge one first"):
        store.apply_ops(_uid(), [{"op": "create_entry", "by": BY, "why": "x",
                                  "entry": {**_hyp(links=[]), "id": None}}])


# ----------------------------------------------------------------------------- append-only
def test_append_only_is_checked_against_the_file(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    old = _create(store, [_measured(ev), _hyp()])
    cases = []
    x = copy.deepcopy(old); x["entries"][0]["observations"][0]["numbers"]["max_dev_pct"] = 9.9
    cases.append((x, "observations[O1]: changed"))
    x = copy.deepcopy(old); x["entries"][0]["observations"].pop(1)
    cases.append((x, "observations[O2]: removed"))
    x = copy.deepcopy(old); x["entries"].pop(1)
    cases.append((x, "removed (entries are never deleted"))
    x = copy.deepcopy(old); x["entries"][0]["revisions"][0]["why"] = "rewritten"
    cases.append((x, "revisions: append-only"))
    x = copy.deepcopy(old); x["entries"][0]["statement"] = "A quietly different statement about the teeth."
    cases.append((x, "changed without a revision"))
    x = copy.deepcopy(old); x["aliases"] = ["Other"]
    cases.append((x, "aliases: 'Test Panel 32' was removed"))
    for new, match in cases:
        errs = check_append_only(old, new)
        assert any(match in e for e in errs), (match, errs)


def test_statement_and_scope_edits_keep_the_old_text(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    old = _create(store, [_measured(ev), _hyp()])
    doc = store.apply_ops(_uid(), [
        {"op": "edit_statement", "by": BY, "why": "clearer", "id": "F-0001",
         "statement": "Over codes 118-190, full field, the light rises in teeth; chromaticity stays flat."},
        {"op": "set_scope", "by": BY, "why": "window measured", "id": "F-0001",
         "scope": {**old["entries"][0]["scope"], "unknown": ["HDR"]}}], today=TODAY)
    f1 = doc["entries"][0]
    assert f1["revisions"][1]["old"]["statement"] == old["entries"][0]["statement"]
    assert f1["revisions"][2]["old"]["scope"] == old["entries"][0]["scope"]
    assert f1["rev"] == 3 and check_append_only(old, doc) == []


def test_status_changes_append_history_and_supersede_names_the_successor(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    _create(store, [_measured(ev), _hyp()])
    doc = store.apply_ops(_uid(), [{"op": "set_status", "by": BY, "why": "window test contradicted it",
                                    "id": "H-0001", "status": "refuted"}], today=TODAY)
    h = doc["entries"][1]
    assert [s["status"] for s in h["status_history"]] == ["hypothesis", "refuted"]
    with pytest.raises(KnowledgeError, match="names its successor"):
        store.apply_ops(_uid(), [{"op": "set_status", "by": BY, "why": "x", "id": "F-0001", "status": "superseded"}])
    with pytest.raises(KnowledgeError, match="already refuted"):
        store.apply_ops(_uid(), [{"op": "set_status", "by": BY, "why": "x", "id": "H-0001", "status": "refuted"}])


def test_open_questions_close_with_an_answer_and_stay(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    _create(store, [_measured(ev), _hyp()])
    doc = store.apply_ops(_uid(), [{"op": "close_open_question", "by": BY, "why": "measured", "id": "F-0001",
                                    "qid": "Q1", "answer": "the window shows the same teeth"},
                                   {"op": "add_open_question", "by": BY, "why": "next", "id": "F-0001",
                                    "q": "HDR?", "test": "PQ codes", "owner_action": False}], today=TODAY)
    qs = doc["entries"][0]["open_questions"]
    assert [(q["id"], q["status"]) for q in qs] == [("Q1", "closed"), ("Q2", "open"), ("Q3", "open")]
    with pytest.raises(KnowledgeError, match="closed already"):
        store.apply_ops(_uid(), [{"op": "close_open_question", "by": BY, "why": "x", "id": "F-0001", "qid": "Q1",
                                  "answer": "again"}])


# ----------------------------------------------------------------------------- rollup
def test_rollup_folds_only_matching_observations_and_keeps_contradictions(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    _create(store, [_measured(ev), _hyp()])
    ops = []
    for i in range(50):
        verdict = "contradicts" if i % 10 == 3 else ("refines" if i % 10 == 7 else "matches")
        ops.append({"op": "add_observation", "by": "flow:test", "why": "re-observed", "id": "F-0001",
                    "observation": {"date": f"2026-12-{1 + i % 28:02d}", "verdict": verdict, "independent": False,
                                    "source": {"kind": "probe", "path": str(ev)},
                                    "numbers": {"max_dev_pct": 3.0 + i / 100, "teeth_pct": [2, 4 + i / 100]}}})
    doc = store.apply_ops(_uid(), ops, today=TODAY)
    f1 = doc["entries"][0]
    kept = f1["observations"]
    roll = f1["rollup"]
    assert len(kept) == kn.MAX_FULL_OBSERVATIONS
    assert roll["count"] == 52 - 40
    assert set(roll["verdicts"]) <= {"matches", "consistent"}
    # every contradicting / refining / establishing observation is still there in full
    assert sum(1 for o in kept if o["verdict"] == "contradicts") == 5
    assert sum(1 for o in kept if o["verdict"] == "refines") == 5
    assert any(o["verdict"] == "establishes" for o in kept)
    assert roll["numbers"]["max_dev_pct"][0] <= roll["numbers"]["max_dev_pct"][1]
    assert roll["numbers"]["teeth_pct"][0] == 2
    assert validate_unit(doc) == []
    st = entry_state(doc, f1, TODAY)
    assert st["observations"] == 52


def test_nothing_foldable_keeps_everything(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    _create(store, [_measured(ev), _hyp()])
    ops = [{"op": "add_observation", "by": BY, "why": "x", "id": "F-0001",
            "observation": {"verdict": "contradicts", "independent": False, "source": {"kind": "p", "ref": "r"},
                            "numbers": {"dev": 7.6}}} for _ in range(45)]
    doc = store.apply_ops(_uid(), ops, today=TODAY)
    f1 = doc["entries"][0]
    # only the one 'consistent' observation (O2) could fold; the 45 contradictions and the establishing read stay
    assert len(f1["observations"]) == 46 and f1["rollup"]["folded_ids"] == ["O2"]
    assert sum(o["verdict"] == "contradicts" for o in f1["observations"]) == 45


# ----------------------------------------------------------------------------- derived state
def test_explain_eligibility_and_unconfirmed_after_a_unit_event(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    doc = _create(store, [_measured(ev), _hyp()])
    f1, h1 = doc["entries"]
    assert entry_state(doc, f1, TODAY)["can_explain"] is True
    st = entry_state(doc, h1, TODAY)
    assert not st["can_explain"] and "status hypothesis" in st["cannot_explain_why"]
    doc = store.apply_ops(_uid(), [{"op": "add_unit_event", "by": "owner", "why": "told the LLM",
                                    "event": {"date": "2026-11-20", "what": "firmware update"}}], today="2026-11-20")
    st = entry_state(doc, doc["entries"][0], "2026-11-21")
    assert st["unconfirmed"] and not st["can_explain"]
    assert entry_state(doc, doc["entries"][0], "2027-07-01")["unconfirmed_why"][-1].startswith("not re-observed")
    single = _measured(ev, links=[], observations=[_measured(ev)["observations"][0]])
    assert entry_state({"unit_events": []}, {**single, "last_confirmed": TODAY},
                       TODAY)["cannot_explain_why"] == ["1 independent confirming session(s), 2 needed"]


def _obs(verdict: str, date: str, **kw) -> dict:
    path = kw.pop("path", None)
    src = {"kind": "probe", "path": str(path)} if path else {"kind": "probe", "ref": kw.pop("ref", "r")}
    kw.pop("ref", None)
    return {"date": date, "verdict": verdict, "independent": kw.pop("independent", False),
            "source": src, "numbers": {"max_dev_pct": 3.0}, **kw}


def test_only_establishing_and_matching_observations_confirm(tmp_path: Path, ev: Path):
    """Review fix 3: 'consistent', 'refines', 'inconclusive' and 'contradicts' never count toward excuse power."""
    store = _store(tmp_path)
    m = _measured(ev, links=[], observations=[_measured(ev)["observations"][0]])
    _create(store, [m])
    ops = [{"op": "add_observation", "by": BY, "why": "x", "id": "F-0001", "observation": _obs(v, d, independent=True)}
           for v, d in (("consistent", "2026-11-01"), ("refines", "2026-11-02"), ("inconclusive", "2026-11-03"))]
    doc = store.apply_ops(_uid(), ops, today=TODAY)
    st = entry_state(doc, doc["entries"][0], TODAY)
    assert st["independent"] == 1 and not st["can_explain"]
    assert doc["entries"][0]["last_confirmed"] == "2026-11-14"     # only confirming verdicts move it


def test_a_newer_contradiction_switches_excuse_power_off_until_resolved(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    _create(store, [_measured(ev, links=[])])
    doc = store.load(_uid())
    assert entry_state(doc, doc["entries"][0], TODAY)["can_explain"] is True
    doc = store.apply_ops(_uid(), [{"op": "add_observation", "by": BY, "why": "x", "id": "F-0001",
                                    "observation": _obs("contradicts", "2026-11-20", independent=True)}], today=TODAY)
    st = entry_state(doc, doc["entries"][0], "2026-11-21")
    assert not st["can_explain"] and st["contradicted_by"] == "O3"
    assert any("contradicted by O3" in w for w in st["cannot_explain_why"])
    # an older contradiction than the last confirmation does not switch it off
    doc = store.apply_ops(_uid(), [{"op": "add_observation", "by": BY, "why": "x", "id": "F-0001",
                                    "observation": _obs("matches", "2026-11-22", independent=True)}], today=TODAY)
    assert entry_state(doc, doc["entries"][0], "2026-11-23")["can_explain"] is True


def test_independence_is_derived_from_session_and_run_not_believed(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    one = _measured(ev, links=[], observations=[
        _obs("establishes", "2026-11-14", path=ev, ref="a", run="runs/probes/20261114_071452_full_stack_spots_x"),
        _obs("matches", "2026-11-14", ref="b", run="runs/probes/20261114_124156_full_stack_spots_y")])
    # same date, no session labels: ONE session, whatever the flags say
    doc = _create(store, [one])
    assert entry_state(doc, doc["entries"][0], TODAY)["independent"] == 1
    # a claim of independence from the same session is refused
    bad = _measured(ev, links=[], observations=[_obs("establishes", "2026-11-14", path=ev, independent=True),
                                                _obs("matches", "2026-11-14", independent=True)])
    with pytest.raises(KnowledgeError, match="shares the session or the run of O1"):
        _create(_store(tmp_path / "b"), [bad])
    # session labels (the meter was re-placed) make two sessions on one day; one run is one session whatever its labels
    two = _measured(ev, links=[], observations=[
        _obs("establishes", "2026-11-14", path=ev, independent=True, session="meter placed before 07:15"),
        _obs("matches", "2026-11-14", independent=True, session="meter re-placed 11:39")])
    doc = _create(_store(tmp_path / "c"), [two])
    assert entry_state(doc, doc["entries"][0], TODAY)["independent"] == 2
    same_run = _measured(ev, links=[], observations=[
        _obs("establishes", "2026-11-14", path=ev, session="a", run="20261114_045100_904510_hdr_x"),
        _obs("matches", "2026-11-15", session="b", run="20261114_045100_904510_hdr_x")])
    doc = _create(_store(tmp_path / "d"), [same_run])
    assert entry_state(doc, doc["entries"][0], TODAY)["independent"] == 1


# ----------------------------------------------------------------------------- the store: corrupt, CAS, lock
def test_a_corrupt_or_invalid_file_is_never_overwritten(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    _create(store, [_measured(ev), _hyp()])
    p = store.path(_uid())
    p.write_text("{ broken", encoding="utf-8")
    with pytest.raises(CorruptFile, match="never overwritten"):
        store.apply_ops(_uid(), [{"op": "add_alias", "by": BY, "why": "x", "alias": "Other"}])
    assert p.read_text(encoding="utf-8") == "{ broken"
    doc = json.loads(json.dumps(_create(_store(tmp_path / "b"), [_measured(ev), _hyp()])))
    doc["entries"][0]["status"] = "probable"
    p.write_text(json.dumps(doc), encoding="utf-8")
    before = p.read_bytes()
    with pytest.raises(CorruptFile) as ei:
        store.apply_ops(_uid(), [{"op": "add_alias", "by": BY, "why": "x", "alias": "Other"}])
    assert any("status: one of" in v for v in ei.value.violations)
    assert p.read_bytes() == before
    assert store.load_tolerant(_uid()) is None          # readers: no knowledge, never a failure


_MALFORMED = {
    "unit_events int": lambda d: d.__setitem__("unit_events", 5),
    "standing_conditions int": lambda d: d.__setitem__("standing_conditions", 7),
    "revisions int": lambda d: d.__setitem__("revisions", 3),
    "aliases str": lambda d: d.__setitem__("aliases", "abc"),
    "identity list": lambda d: d.__setitem__("identity", [1]),
    "entries contain a list": lambda d: d["entries"].append([1, 2]),
    "entries dict": lambda d: d.__setitem__("entries", {"a": 1}),
    "signature type list": lambda d: d["entries"][0]["signature"].__setitem__("type", ["code_structure"]),
    "signature str": lambda d: d["entries"][0].__setitem__("signature", "x"),
    "links int": lambda d: d["entries"][0].__setitem__("links", 5),
    "link rel list": lambda d: d["entries"][0].__setitem__("links", [{"rel": ["x"], "id": "H-0001"}]),
    "facet value int": lambda d: d["entries"][0].__setitem__("facets", {"grey_light_structure": 5}),
    "scope.mode str": lambda d: d["entries"][0]["scope"].__setitem__("mode", "SDR"),
    "scope list": lambda d: d["entries"][0].__setitem__("scope", [1]),
    "rollup verdicts list": lambda d: d["entries"][0].__setitem__(
        "rollup", {"count": 1, "folded_ids": ["O99"], "verdicts": [1]}),
    "rollup folded_ids of lists": lambda d: d["entries"][0].__setitem__(
        "rollup", {"count": 1, "folded_ids": [["O9"]], "verdicts": {}}),
    "observation id list": lambda d: d["entries"][0]["observations"][0].__setitem__("id", ["O1"]),
    "observation source list": lambda d: d["entries"][0]["observations"][0].__setitem__("source", [1]),
    "observations dict": lambda d: d["entries"][0].__setitem__("observations", {"O1": 1}),
    "status_history str": lambda d: d["entries"][0].__setitem__("status_history", "x"),
    "open_questions dict": lambda d: d["entries"][0].__setitem__("open_questions", {"Q1": 1}),
    "revisions of an entry int": lambda d: d["entries"][0].__setitem__("revisions", 4),
    "NaN number": lambda d: d["entries"][0]["observations"][0]["numbers"].__setitem__("x", float("nan")),
}


def test_a_malformed_document_is_a_list_of_violations_never_a_crash(tmp_path: Path, ev: Path):
    """Review fix 4: every malformed shape the reviewer's fuzz found (and more) is reported, never raised."""
    doc = _create(_store(tmp_path), [_measured(ev), _hyp()])
    for name, mut in _MALFORMED.items():
        d = copy.deepcopy(doc)
        mut(d)
        v = validate_unit(d)
        assert isinstance(v, list) and v, name
        st = KnowledgeStore(tmp_path / f"m{abs(hash(name)) % 10 ** 8}")
        st.root.mkdir(parents=True)
        st.path(_uid()).write_text(json.dumps(d, allow_nan=True), encoding="utf-8")
        with pytest.raises(CorruptFile):
            st.read_raw(_uid())
        assert st.load_tolerant(_uid()) is None and _uid() in st.broken


def test_one_broken_file_never_stops_the_other_units_from_resolving(tmp_path: Path, ev: Path, capsys):
    store = _store(tmp_path)
    doc = _create(store, [_measured(ev), _hyp()])
    bad = copy.deepcopy(doc)
    bad["unit_id"] = "BROKEN-UNIT-1"
    bad["unit_events"] = 5
    (store.root / "BROKEN-UNIT-1.json").write_text(json.dumps(bad), encoding="utf-8")
    (store.root / "ALSO-BROKEN-2.json").write_text("{ not json", encoding="utf-8")
    assert store.resolve("Test Panel 32") == _uid()
    assert set(store.broken) == {"BROKEN-UNIT-1", "ALSO-BROKEN-2"}
    with pytest.raises(UnitNotFound, match=r"BROKEN-UNIT-1 \(unreadable"):
        store.resolve("Nobody")
    assert [p.name for p in store.render_all(today=TODAY)] == [f"{_uid()}.md"]
    # the CLI: validate reports invalid (1), a refusal is 2, a crash is 3; the broken files are named
    from dlc import knowledge_cli as cli
    base = ["--dir", str(store.root), "--today", TODAY]
    assert cli._main([*base, "validate"]) == cli.EXIT_INVALID
    out = capsys.readouterr()
    assert "INVALID BROKEN-UNIT-1" in out.out and f"ok {_uid()}" in out.out
    assert cli._main([*base, "show", "--display", "Test Panel 32"]) == cli.EXIT_OK
    assert "BROKEN-UNIT-1.json is broken and was skipped" in capsys.readouterr().err
    assert cli._main([*base, "show", "--display", "Nobody"]) == cli.EXIT_REFUSED


def test_an_unforeseen_exception_is_reported_as_a_crash(tmp_path: Path, monkeypatch, capsys):
    from dlc import knowledge_cli as cli

    def boom(args, store):
        raise RuntimeError("a defect")

    monkeypatch.setattr(cli, "_run", boom)
    assert cli._main(["--dir", str(tmp_path), "validate"]) == cli.EXIT_CRASHED
    assert "CRASHED" in capsys.readouterr().err


def test_a_malformed_op_is_refused_not_a_crash(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    _create(store, [_measured(ev), _hyp()])
    for op in ({"op": "add_link", "by": BY, "why": "x", "id": "F-0001", "link": 5},
               {"op": "add_observation", "by": BY, "why": "x", "id": "F-0001", "observation": [1]},
               {"op": "set_scope", "by": BY, "why": "x", "id": "F-0001", "scope": 7},
               {"op": "add_unit_event", "by": BY, "why": "x", "event": [1]},
               {"op": "set_standing_condition", "by": BY, "why": "x", "condition": {"setting": 5}}):
        with pytest.raises(KnowledgeError):
            store.apply_ops(_uid(), [op])
        assert kn.validate_ops(store.load(_uid()), [op])


def test_cas_on_rev_reapplies_once_then_refuses(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    _create(store, [_measured(ev), _hyp()])
    other = KnowledgeStore(store.root)
    # a stale base rev: re-applied onto the current file (the one re-apply)
    other.apply_ops(_uid(), [{"op": "add_alias", "by": BY, "why": "x", "alias": "Second name"}], today=TODAY)
    doc = store.apply_ops(_uid(), [{"op": "add_alias", "by": BY, "why": "x", "alias": "Third name"}], base_rev=1,
                          today=TODAY)
    assert doc["rev"] == 3 and doc["aliases"] == ["Test Panel 32", "Second name", "Third name"]

    # a writer that lands between this write's read and its lock, every time: one retry, then refused
    calls = {"n": 0}

    def racing(d: dict) -> None:
        calls["n"] += 1
        other.apply_ops(_uid(), [{"op": "add_alias", "by": BY, "why": "x", "alias": f"race {calls['n']}"}],
                        today=TODAY)
        apply_op(d, {"op": "add_alias", "by": BY, "why": "x", "alias": "mine"}, today=TODAY)

    with pytest.raises(ConcurrentWrite):
        store.write(_uid(), racing, today=TODAY)
    assert calls["n"] == 2
    final = store.load(_uid())
    assert "mine" not in final["aliases"] and "race 2" in final["aliases"]

    # a stale base AND a race: no second chance
    calls["n"] = 0
    with pytest.raises(ConcurrentWrite):
        store.write(_uid(), racing, base_rev=1, today=TODAY)
    assert calls["n"] == 1


def test_validate_ops_is_a_dry_run(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    doc = _create(store, [_measured(ev), _hyp()])
    before = store.path(_uid()).read_bytes()
    assert kn.validate_ops(doc, [{"op": "add_open_question", "by": BY, "why": "x", "id": "F-0001", "q": "q?",
                                  "test": "t", "owner_action": False}]) == []
    errs = kn.validate_ops(doc, [{"op": "set_status", "by": BY, "why": "x", "id": "F-0001", "status": "superseded"}])
    assert any("names its successor" in e for e in errs)
    assert store.path(_uid()).read_bytes() == before


# ----------------------------------------------------------------------------- resolution
def test_resolution_by_alias_dip_key_edid_device_path_and_unit_id(tmp_path: Path, ev: Path):
    store = _store(tmp_path)
    _create(store, [_measured(ev), _hyp()])
    for name in ("Test Panel 32", "test  panel 32", "Test Panel 32:SDR", "Test Panel 32:hdr", "TST1234-12345",
                 r"\\?\DISPLAY#TST1234#5&1&UID1#{x}", _uid()):
        assert store.resolve(name) == _uid(), name
    with pytest.raises(UnitNotFound, match=r"known: TST1234-12345 \(Test Panel 32\)"):
        store.resolve("Other Panel")
    with pytest.raises(UnitNotFound, match="empty"):
        KnowledgeStore(tmp_path / "nothing").resolve("Test Panel 32")
    # a second unit with the same alias makes the name ambiguous
    h = _header()
    h["identity"] = {"hardware_id": "TST1234", "edid_id": "TST1234-99999"}
    hdr = kn.unit_from_header(h)
    store.apply_ops(hdr["unit_id"], [{"op": "create_entry", "by": BY, "why": "x", "entry": _hyp(links=[])}],
                    create=hdr)
    with pytest.raises(KnowledgeError, match="more than one unit"):
        store.resolve("Test Panel 32")
    assert store.resolve("TST1234-99999") == "TST1234-99999"


def test_relative_evidence_resolves_against_the_dip_store_folder_and_folders_hash(tmp_path: Path):
    (tmp_path / "runs" / "r1").mkdir(parents=True)
    (tmp_path / "runs" / "r1" / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "runs" / "r1" / "b.txt").write_text("b", encoding="utf-8")
    store = KnowledgeStore.beside(tmp_path / "dip_store.json")
    assert store.root == tmp_path / "dip_knowledge" and store.base_dir == tmp_path
    src = kn.hash_evidence({"kind": "run", "path": "runs/r1#raw.ti3"}, store.base_dir)
    assert src["hash_of"] == "dir" and src["path"] == "runs/r1#raw.ti3"
    assert src["sha256"] == kn.sha256_dir(tmp_path / "runs" / "r1")
    (tmp_path / "runs" / "r1" / "b.txt").write_text("c", encoding="utf-8")
    assert kn.sha256_dir(tmp_path / "runs" / "r1") != src["sha256"]


# ----------------------------------------------------------------------------- the view
def _golden_doc(tmp_path: Path) -> tuple[dict, str]:
    ev = tmp_path / "evidence" / "dense.json"
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_bytes(b'{"tables": "raw reads live here"}\n')     # bytes: the same sha256 on every OS
    store = KnowledgeStore(tmp_path / "dip_knowledge", base_dir=tmp_path)
    m = _measured(ev)
    m["observations"][0]["source"]["path"] = "evidence/dense.json"
    hdr = kn.unit_from_header(_header())
    ops = [{"op": "set_standing_condition", "by": "owner", "why": "the owner's everyday menu",
            "condition": {"setting": "local dimming", "value": "Fast",
                          "source": "as stated by the owner 2026-10, unverified by DLC", "verified": False}}]
    ops += [{"op": "create_entry", "by": BY, "why": "first entries", "entry": e} for e in (m, _hyp(), _event())]
    ops += [{"op": "create_entry", "by": BY, "why": "a refuted idea",
             "entry": {"kind": "rig", "title": "The meter drifts warm", "status": "hypothesis",
                       "statement": "The colorimeter's own reading drifts as it warms up on the panel.",
                       "scope": {"instrument": "i1D3", "unknown": ["other meters"]}}},
            {"op": "set_status", "by": BY, "why": "a cold-meter A-B showed no drift", "id": "R-0001",
             "status": "refuted"}]
    doc = store.apply_ops(hdr["unit_id"], ops, create=hdr, today=TODAY)
    return doc, store.md_path(hdr["unit_id"]).read_text(encoding="utf-8")


def test_rendered_view_matches_the_golden_file(tmp_path: Path):
    doc, md = _golden_doc(tmp_path)
    assert md == render(doc, source=f"{_uid()}.json", today=TODAY)
    if os.environ.get("DLC_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(md, encoding="utf-8", newline="\n")
    assert md == GOLDEN.read_text(encoding="utf-8")
    # order: open questions first, then panel, rig, events, refuted last
    heads = [ln for ln in md.splitlines() if ln.startswith("## ")]
    assert heads == ["## Open questions", "## Panel", "## Rig", "## Events",
                     "## Refuted and superseded (kept so nobody re-proposes them)"]
    assert md.index("R-0001") > md.index("## Refuted")
    assert "(needs the owner)" in md


def test_render_filters_by_mode_and_id(tmp_path: Path):
    doc, _ = _golden_doc(tmp_path)
    hdr_only = render(doc, today=TODAY, mode="HDR")
    assert "E-0001" in hdr_only and "F-0001 —" not in hdr_only
    one = render(doc, today=TODAY, only="H-0001")
    assert "### H-0001" in one and "### F-0001" not in one
