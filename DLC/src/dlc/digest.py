"""The LLM-facing digest projection of a run's spine — the boundary view, no firehose.

``python -m dlc.digest --run <dir>`` prints the **digest-tier** events of a run's
``events.jsonl`` as JSON: the run header, phase changes, stage boundaries, seams,
anomalies, optimizer iterations, scored metrics, the measure-stage outcomes, the
progress-driven check-ins, stalls, and the terminal ``run_done``. The per-patch
``patch_read`` / ``heartbeat`` / ``progress`` firehose is dropped (it's the
dashboard's job, not the LLM's), so this is exactly what an assistant reads to
**check in on a long run** without tailing thousands of reads.

This is the read path that closes the "LLM is blind mid-run" gap: a supervising
assistant can pull it at any moment (or after a ``--cancel``) to see where the run
is. Dependency-free (spine only), like the rest of the core.

Pairs with the dashboard's ``/api/digest`` endpoint (same projection, live) and the
cooperative ``dlc-calibrate --cancel`` control channel (the *act* on what you see).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

from .events import digest_projection, read_events


def project(events_path: Path) -> list[dict[str, Any]]:
    """The digest-tier events of one run, as flat JSON-friendly dicts (firehose dropped)."""
    return [{"time": e.time, "level": e.level, "stage": e.stage, "phase": e.phase,
             "event": e.event, "data": e.data}
            for e in digest_projection(read_events(events_path))]


def _events_path(run: Path) -> Path:
    """Accept either a run dir or a direct ``events.jsonl`` path."""
    return run if run.suffix == ".jsonl" else run / "events.jsonl"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="dlc-digest",
                                 description="LLM-facing digest projection of a DLC run (no firehose)")
    ap.add_argument("--run", type=Path, required=True,
                    help="run dir (or a direct events.jsonl path)")
    ap.add_argument("--tail", type=int, default=0,
                    help="only the last N digest events (0 = all)")
    args = ap.parse_args(argv)

    path = _events_path(args.run)
    items = project(path)
    if args.tail > 0:
        items = items[-args.tail:]
    print(json.dumps({"events_path": str(path), "count": len(items), "digest": items}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
