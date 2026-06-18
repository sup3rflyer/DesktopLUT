"""``python -m dlc.dashboard`` — run the mission-control dashboard.

By default it follows ``runs/active.json`` (the producer-written pointer), so you can
start it once and leave it up across runs — it moves to each new run on its own and keeps
showing the last one in between. Point it at a specific run with ``--run`` to review a
finished (or in-flight) run in isolation.
"""

from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from ..paths import RUNS_DIR
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dlc-dashboard",
                                     description="Live mission-control dashboard for a DLC run.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--run", type=Path, default=None,
                       help="Watch one run folder (or its events.jsonl) instead of following active.json.")
    group.add_argument("--runs-dir", type=Path, default=RUNS_DIR,
                       help=f"Runs root to follow via active.json (default: {RUNS_DIR}).")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default 8765).")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in a browser.")
    args = parser.parse_args(argv)

    if args.open:
        try:
            webbrowser.open(f"http://{args.host}:{args.port}")
        except Exception:  # noqa: BLE001 - opening a browser is a nicety, never fatal
            pass

    if args.run is not None:
        serve(run=args.run, host=args.host, port=args.port)
    else:
        serve(runs_dir=args.runs_dir, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
