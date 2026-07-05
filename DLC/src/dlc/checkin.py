"""§12 timed check-in assembly — the NON-BLOCKING evidence packet for the overseeing LLM.

Moved verbatim out of ``calibrate.py`` in the fable Phase 8 audit (Phase 7b RFC item R2:
a narrow-coupling block extracted BEFORE the evidence-packet redesign, so the redesign
lands here instead of churning the orchestrator twice). The functions take the live
:class:`~dlc.calibrate.Calibration` orchestrator (``cal``) — the check-in *state* (window
clock, tally snapshot, events byte offset, latest-metric snapshots) deliberately stays on
the orchestrator, where the stages that feed it live; this module owns the *assembly*.

DESIGN LAW (do not regress): a check-in NEVER pauses the spine and carries NO
recommendation/accept for anyone to rubber-stamp. It is the spine collecting the evidence
since the last check-in — warnings, the max ΔE actually read, re-read / repeated patches,
non-stopper anomalies — and handing it to the LLM EVERY TIME so the LLM applies judgment
("ΔE high but that's the panel limit"; "patch re-read twice but the latest read is normal
→ self-corrected") and intervenes ONLY if it sees a real problem. Run-stoppers are a
SEPARATE mechanism (adjudicated seams — :mod:`dlc.adjudication`). The LLM consumes this
from the running (background) spine out of band — emit-only, no exit-10 gate. This is the
point of DLC: LLM intelligence consuming tools+data, not a deterministic program.

Spine-tier: stdlib only, imports nothing from the rest of DLC.
"""

from __future__ import annotations

import json
import time
from typing import Any

# Severity rank for the evidence packet's warning list (lower = more severe = kept first
# when the inline cap truncates): a stall is a run-threatening event, an anomaly is a
# threshold ping, a read-plausibility anomaly is the most routine of the three.
_WARNING_SEVERITY = {"stall": 0, "anomaly": 1, "read_plausibility_anomaly": 2}

# The NO-DARK-WINDOW ceiling (owner rule, 2026-07-05 / fable Phase 8): on an
# LLM-ADJUDICATED run (anything but the sim/CI AutoAdjudicator) there must never be a
# window longer than this without a check-in while the spine is executing — a 5-hour
# measure phase that is only looked at at its start and end is exactly what §12 exists
# to prevent. Enforced at the Calibration ctor: a disabled (0) or longer interval is
# clamped to this ceiling on adjudicated runs (--auto keeps the free choice — a
# rubber-stamped sim run has no LLM watching). The cadence is delivered by wall-clock
# backstops on EVERY long path: the measure loop's read funnel + soak blocks
# (measure_loop._Loop), the optimizer's per-probe-read hook, and characterize's
# instrumented reads (calibrate.py).
NO_DARK_WINDOW_CEILING_S = 1200.0

__all__ = [
    "maybe_timed_checkin",
    "checkin_digest",
    "checkin_evidence",
    "run_overview",
    "events_since_last_checkin",
    "latest_checkin_metrics",
    "events_size",
]


def maybe_timed_checkin(cal: Any, trigger: str) -> None:
    """Emit a rich evidence packet for the overseeing LLM once the wall-clock floor has
    elapsed (§12). Disabled at interval 0; the first checkpoint only anchors the clock.
    See the module docstring's DESIGN LAW — emit-only, never a gate."""
    if cal._checkin_interval_s <= 0 or cal.runlog is None:
        return
    now = time.monotonic()
    if cal._last_checkin_monotonic is None:
        # First checkpoint just anchors the clock — no immediate ping at second 0.
        cal._last_checkin_monotonic = now
        cal._last_checkin_tally = dict(cal.runlog.tally)
        cal._last_checkin_pos = events_size(cal)
        return
    if now - cal._last_checkin_monotonic < cal._checkin_interval_s:
        return
    elapsed_since = now - cal._last_checkin_monotonic
    seq = int(cal.calib.get("checkin_seq", 0)) + 1
    cal.calib["checkin_seq"] = seq
    digest = checkin_digest(cal, trigger, seq=seq,
                            elapsed_since_checkin_s=round(elapsed_since, 1))
    # Reset the window AFTER building the digest, BEFORE emitting, so the next window starts
    # clean and the check_in event itself isn't counted into it.
    cal._last_checkin_monotonic = now
    cal._last_checkin_tally = dict(cal.runlog.tally)
    cal._last_checkin_pos = events_size(cal)
    cal.runlog.check_in(trigger, **digest)   # EMIT-ONLY: evidence for the LLM, never a gate


def checkin_digest(cal: Any, trigger: str, *, seq: int = 0,
                   elapsed_since_checkin_s: float = 0.0) -> dict[str, Any]:
    """The rich check-in payload: the run overview, what happened since the last check-in,
    and the latest live metrics — exactly what a supervising LLM needs to judge "continue?"."""
    return {
        "seq": seq,
        "elapsed_since_checkin_s": elapsed_since_checkin_s,
        "overview": run_overview(cal, trigger),
        "since_last": events_since_last_checkin(cal),
        "evidence": checkin_evidence(cal),
        "metrics": latest_checkin_metrics(cal),
    }


def events_size(cal: Any) -> int:
    """Current byte size of events.jsonl (the check-in evidence window high-water mark)."""
    try:
        return cal.runlog.path.stat().st_size if cal.runlog else 0
    except OSError:
        return 0


def checkin_evidence(cal: Any) -> dict[str, Any]:
    """The REAL evidence since the last check-in, read back from the events.jsonl window:
    every warning/anomaly (with detail), the max ΔE actually read + which patch, and the
    read count. This is data for the LLM to JUDGE — deliberately NOT a verdict and NOT a
    recommendation. The full firehose is always on disk; this is the at-a-glance packet."""
    out: dict[str, Any] = {"reads": 0, "max_dE": None, "max_dE_patch": None, "warnings": []}
    if cal.runlog is None:
        return out
    try:
        with cal.runlog.path.open("r", encoding="utf-8") as fh:
            fh.seek(cal._last_checkin_pos or 0)
            lines = fh.readlines()
    except OSError:
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        ev = e.get("event")
        data = e.get("data") or {}
        if ev == "patch_read":
            out["reads"] += 1
            de = data.get("dE")
            if isinstance(de, (int, float)) and (out["max_dE"] is None or de > out["max_dE"]):
                out["max_dE"] = round(de, 3)
                out["max_dE_patch"] = data.get("label") or data.get("role") or data.get("signal")
        elif ev in ("anomaly", "read_plausibility_anomaly", "stall"):
            w = {"event": ev, "stage": e.get("stage")}
            for k in ("kind", "label", "reason", "message", "detail", "attempt"):
                if k in data:
                    w[k] = data[k]
            out["warnings"].append(w)
    # Per-type totals BEFORE any truncation, so the cap can never hide the SCALE of a
    # problem (25 shown of 400 read-anomalies is a different judgment than 25 of 26).
    counts: dict[str, int] = {}
    for w in out["warnings"]:
        counts[w["event"]] = counts.get(w["event"], 0) + 1
    if counts:
        out["warning_counts"] = counts
    # Worst-first, then cap (fable Phase 8, from the 7a lead): a stall outranks an anomaly
    # outranks a read-plausibility ping, so truncation drops the LEAST severe events — the
    # old arrival-order cap could bury the one stall under 25 routine read anomalies. The
    # sort is stable, so chronology is preserved within each severity class (the "re-read
    # twice but the latest is normal" judgment still reads in order).
    out["warnings"].sort(key=lambda w: _WARNING_SEVERITY.get(w.get("event"), 9))
    if len(out["warnings"]) > 25:
        extra = len(out["warnings"]) - 25
        out["warnings"] = out["warnings"][:25] + [{"truncated": extra, "note": "see events.jsonl"}]
    return out


def run_overview(cal: Any, trigger: str) -> dict[str, Any]:
    stages = cal.calib.get("stages") or {}
    done = [k for k, v in stages.items() if (v or {}).get("status") == "done"]
    elapsed = None
    if cal._run_started_monotonic is not None:
        elapsed = round(time.monotonic() - cal._run_started_monotonic, 1)
    return {
        "run": cal.ctx.root.name,
        "flow": cal.calib.get("flow"),
        "mode": cal.mode,
        "target": cal.target_name,
        "phase": cal.runlog.phase if cal.runlog else None,
        "stage": trigger,
        "stages_done": len(done),
        "completed": done,
        "elapsed_s": elapsed,
    }


def events_since_last_checkin(cal: Any) -> dict[str, int]:
    """Per-event-name counts emitted since the previous check-in (anomalies, seams, reads,
    optimizer iterations, …) — the spine delta, computed from the RunLog tally, no disk read."""
    cur = cal.runlog.tally if cal.runlog else {}
    prev = cal._last_checkin_tally or {}
    return {name: cur[name] - prev.get(name, 0)
            for name in cur if cur[name] - prev.get(name, 0) > 0}


def latest_checkin_metrics(cal: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if cal._last_scored:
        out["last_scored"] = cal._last_scored
    if cal._last_optimizer:
        out["optimizer"] = cal._last_optimizer
    if cal._last_refine:
        out["refine"] = cal._last_refine
    if cal._last_bookend_drift:
        out["bookend_drift"] = {
            k: cal._last_bookend_drift.get(k)
            for k in ("available", "role", "metric", "max_delta_de", "p95_delta_de",
                      "mean_delta_de", "threshold", "unique_signals")
            if k in cal._last_bookend_drift
        }
    return out
