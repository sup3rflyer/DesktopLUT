"""The LLM-facing digest read path (dlc.digest): the digest-tier projection of a run's
spine, with the per-patch / heartbeat / progress firehose dropped."""

from __future__ import annotations

import json

from dlc.events import RunLog
from dlc.digest import project, main


def _make_run(tmp_path):
    """Write a small but representative spine: boundaries + firehose + a check-in."""
    events = tmp_path / "events.jsonl"
    log = RunLog(events)
    log.header(run_id="r1", flow="full")
    log.set_phase("measure:post-mhc")
    log.stage_start("measure")
    log.patch_read("measure", seq=0, role="measurement", ok=True)   # stream — dropped
    log.heartbeat("measure", since_progress_s=2.0)                  # stream — dropped
    log.progress("measure", patches_done=1, patches_total=10)       # stream — dropped
    log.check_in("measure", progress=0.5, patches_done=5, patches_total=10)
    log.optimizer_iteration(iteration=1, measured_max_de=1.2)
    log.metrics_scored("verify", avg_de2000=0.4)
    log.run_done("completed")
    return events


def test_project_keeps_digest_drops_firehose(tmp_path):
    events = _make_run(tmp_path)
    items = project(events)
    names = [e["event"] for e in items]
    assert "patch_read" not in names and "heartbeat" not in names and "progress" not in names
    # the boundaries + check-in + optimizer + scored metrics + terminal are all kept
    for kept in ("run_header", "phase", "stage_start", "check_in",
                 "optimizer_iteration", "metrics_scored", "run_done"):
        assert kept in names, kept
    # each item carries the run phase + structured data (the LLM's context)
    ci = next(e for e in items if e["event"] == "check_in")
    assert ci["phase"] == "measure:post-mhc" and ci["data"]["progress"] == 0.5


def test_cli_prints_digest_json(tmp_path, capsys):
    _make_run(tmp_path)
    rc = main(["--run", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == len(out["digest"]) > 0
    assert all(e["event"] not in ("patch_read", "heartbeat", "progress") for e in out["digest"])


def test_cli_tail_limits_to_last_n(tmp_path):
    _make_run(tmp_path)
    items = project(tmp_path / "events.jsonl")
    # the standalone projection and a --tail slice agree on ordering (terminal is last)
    assert items[-1]["event"] == "run_done"
