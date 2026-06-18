"""Tests for the unified event spine (dlc.events): the one log feeding both the
dashboard (full tail) and the LLM (digest projection)."""

from __future__ import annotations

from dlc.events import (
    Ev,
    Event,
    EventWriter,
    RunLog,
    derive_tier,
    digest_projection,
    read_events,
)


# --------------------------------------------------------------------------
# Backward compatibility — the long-standing EventWriter API still works
# --------------------------------------------------------------------------
def test_legacy_write_signature_roundtrips(tmp_path):
    path = tmp_path / "events.jsonl"
    EventWriter(path).write("INFO", "preflight", "checked", monitor=0, ok=True)
    events = read_events(path)
    assert len(events) == 1
    assert events[0].event == "checked"
    assert events[0].stage == "preflight"
    assert events[0].data == {"monitor": 0, "ok": True}
    # No explicit tier/phase on a legacy event — both are None (derived on demand).
    assert events[0].tier is None
    assert events[0].phase is None


def test_writer_appends_never_truncates(tmp_path):
    path = tmp_path / "events.jsonl"
    w = EventWriter(path)
    w.write("INFO", "a", "one")
    w.write("INFO", "b", "two")
    assert len(read_events(path)) == 2


def test_read_events_skips_half_written_final_line(tmp_path):
    path = tmp_path / "events.jsonl"
    EventWriter(path).write("INFO", "a", "one")
    # Simulate a live tail catching a partial trailing line.
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"level":"INFO","stage":"b","eve')
    events = read_events(path)
    assert len(events) == 1
    assert events[0].event == "one"


# --------------------------------------------------------------------------
# Tier derivation — the digest/stream split
# --------------------------------------------------------------------------
def test_stream_events_are_stream_tier():
    for name in (Ev.PATCH_READ, Ev.HEARTBEAT, Ev.PROGRESS):
        assert derive_tier(name, "INFO") == "stream"


def test_major_events_are_digest_tier():
    for name in (Ev.RUN_HEADER, Ev.PHASE, Ev.STAGE_START, Ev.SEAM, Ev.STALL, Ev.NOTE):
        assert derive_tier(name, "INFO") == "digest"


def test_warn_or_error_always_digest_even_on_stream_event():
    # A failed-read storm (WARN on a patch_read) must reach the LLM.
    assert derive_tier(Ev.PATCH_READ, "WARN") == "digest"
    assert derive_tier(Ev.HEARTBEAT, "ERROR") == "digest"


def test_effective_tier_uses_explicit_then_derives():
    explicit = Event(level="INFO", stage="s", event=Ev.PATCH_READ, tier="digest")
    assert explicit.effective_tier == "digest"   # explicit wins
    derived = Event(level="INFO", stage="s", event=Ev.PATCH_READ)
    assert derived.effective_tier == "stream"    # derived from the name


# --------------------------------------------------------------------------
# RunLog — phase stamping + typed helpers
# --------------------------------------------------------------------------
def test_runlog_stamps_active_phase_on_every_event(tmp_path):
    path = tmp_path / "events.jsonl"
    log = RunLog(path)
    log.set_phase("measure:raw")
    log.patch_read("measure", seq=0, rgb=[255, 255, 255], Y=120.0)
    log.heartbeat("measure", elapsed_s=3.0)
    events = read_events(path)
    # The phase event itself + the two stamped events.
    phase_evt = next(e for e in events if e.event == Ev.PHASE)
    assert phase_evt.data["phase_name"] == "measure:raw"
    for e in events:
        if e.event in (Ev.PATCH_READ, Ev.HEARTBEAT):
            assert e.phase == "measure:raw"


def test_typed_helpers_pin_tier(tmp_path):
    path = tmp_path / "events.jsonl"
    log = RunLog(path, phase="p")
    log.header(target="D65 / 2.2", ccmx="i1d3.ccmx")
    log.patch_read("measure", seq=1)
    log.heartbeat("measure")
    log.stall("measure", reason="no progress 600s")
    events = {e.event: e for e in read_events(path)}
    assert events[Ev.RUN_HEADER].tier == "digest"
    assert events[Ev.PATCH_READ].tier == "stream"
    assert events[Ev.HEARTBEAT].tier == "stream"
    assert events[Ev.STALL].tier == "digest"
    assert events[Ev.STALL].level == "ERROR"


# --------------------------------------------------------------------------
# Digest projection — what the LLM sees
# --------------------------------------------------------------------------
def test_digest_projection_drops_firehose_keeps_boundaries(tmp_path):
    path = tmp_path / "events.jsonl"
    log = RunLog(path)
    log.header(target="D65")
    log.set_phase("measure:raw")
    for i in range(50):
        log.patch_read("measure", seq=i)
        log.heartbeat("measure")
    log.stage_done("measure", patches=50)
    log.seam("verify", key="verify:accept")

    all_events = read_events(path)
    digest = digest_projection(all_events)
    names = {e.event for e in digest}

    # The header, phase, stage boundary and seam survive…
    assert {Ev.RUN_HEADER, Ev.PHASE, Ev.STAGE_DONE, Ev.SEAM} <= names
    # …but the 100 firehose records are gone.
    assert Ev.PATCH_READ not in names
    assert Ev.HEARTBEAT not in names
    assert len(digest) < len(all_events)


def test_digest_projection_keeps_warn_on_stream_event(tmp_path):
    path = tmp_path / "events.jsonl"
    w = EventWriter(path)
    w.write("WARN", "measure", Ev.PATCH_READ, error="read failed")  # legacy-style, no tier
    digest = digest_projection(read_events(path))
    assert len(digest) == 1
    assert digest[0].event == Ev.PATCH_READ
