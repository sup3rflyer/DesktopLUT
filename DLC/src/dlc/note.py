"""Post a one-line supervisor note to a run — shown in DesktopLUT's DLC tab feed.

The supervising LLM's judgments ("red max is the panel floor at 75 % — no action") otherwise live
only in the chat; a note puts them where the person watching the run looks. Notes go to
``runs/<id>/notes.jsonl`` — never the spine (``events.jsonl`` has a single writer, the run) — and
the live publisher merges them into the feed by time. Purely informational: the run never reads it.

    python -m dlc.note "red max is the panel floor at 75 %; no action"
    python -m dlc.note --run runs/<id> --stage measure "blue settled after 6 min"

Without ``--run`` the note goes to the run ``runs/active.json`` points at.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .dashboard.publish import NOTES_FILE
from .paths import runs_dir

MAX_CHARS = 300


def active_run() -> Optional[Path]:
    try:
        raw = json.loads((runs_dir() / "active.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    run = raw.get("run") if isinstance(raw, dict) else None
    return Path(run) if run else None


def post_note(run_root: Path, message: str, *, stage: str = "", author: str = "claude") -> Path:
    """Append one note line. The message is collapsed to one line and capped at MAX_CHARS."""
    text = " ".join(str(message).split())[:MAX_CHARS]
    if not text:
        raise ValueError("empty note")
    record = {"time": datetime.now().isoformat(timespec="seconds"), "author": author,
              "stage": stage, "message": text}
    path = Path(run_root) / NOTES_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - thin CLI
    parser = argparse.ArgumentParser(prog="python -m dlc.note", description=__doc__.splitlines()[0])
    parser.add_argument("message", help="the note (one line; longer text is cut)")
    parser.add_argument("--run", type=Path, default=None, help="run directory (default: runs/active.json)")
    parser.add_argument("--stage", default="", help="stage the note is about (optional)")
    parser.add_argument("--author", default="claude")
    args = parser.parse_args(argv)
    run = args.run or active_run()
    if run is None or not run.is_dir():
        print("no run: pass --run or start a run (runs/active.json)")
        return 2
    print(post_note(run, args.message, stage=args.stage, author=args.author))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
