"""Run folder and manifest management."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .events import EventWriter
from .paths import atomic_write_text, runs_dir


@dataclass
class RunManifest:
    """Persistent run metadata."""

    name: str
    mode: str
    display: str | None
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="microseconds"))
    status: str = "created"
    tools: dict[str, str | None] = field(default_factory=dict)
    desktoplut: dict[str, Any] = field(default_factory=dict)
    human_actions: dict[str, Any] = field(default_factory=dict)
    stages: list[dict[str, Any]] = field(default_factory=list)
    # The CLI invocation that STARTED the run (exact argv + cwd — a relative --profile / --run
    # resolves against it), kept as first recorded; every later invocation on the run (a resume
    # with --decide, an --abort) is appended to `resumes`. A resumer relaunches with
    # launch.argv + `--run <dir> --decide KEY=CHOICE`. Empty for in-process runs (no CLI).
    launch: dict[str, Any] = field(default_factory=dict)
    resumes: list[dict[str, Any]] = field(default_factory=list)


def launch_record(argv: Sequence[str], *, cwd: Path | str | None = None,
                  program: str | None = None) -> dict[str, Any]:
    """One CLI invocation as the manifest keeps it: the exact ``argv`` (arguments after the
    program), the working directory it resolves relative paths against, the interpreter, the
    program as invoked (``sys.argv[0]``, when the argv came from the command line), and when."""
    rec: dict[str, Any] = {
        "argv": [str(a) for a in argv],
        "cwd": str(cwd if cwd is not None else os.getcwd()),
        "python": sys.executable,
        "time": datetime.now().isoformat(timespec="seconds"),
        "pid": os.getpid(),
    }
    if program:
        rec["program"] = str(program)
    return rec


@dataclass
class RunContext:
    """Paths and manifest for one calibration run."""

    root: Path
    manifest: RunManifest

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def log_path(self) -> Path:
        return self.root / "workflow.log"

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    # The names ensure_dirs / a run's own machinery may leave in a run root. A pre-existing dir
    # is adoptable (half-created run) ONLY if it contains nothing outside this set — so pointing
    # --run at an arbitrary populated folder is refused instead of scattering run files into it
    # (fable Phase 7a finding F7a-A-runs; the old exist_ok=False raised, which was the guard).
    _RUN_DIR_ALLOWED = frozenset({
        "preflight", "probe_match", "sequences", "measurements", "generated", "reports",
        "manifest.json", "workflow.log", "events.jsonl", "dlc_state.json",
    })

    def ensure_dirs(self) -> None:
        # exist_ok: a crash between this mkdir and the first manifest save leaves a
        # half-created run dir that would otherwise brick BOTH paths — open_run refuses it
        # (no manifest.json) and create_run could not re-create it (exist_ok=False raised).
        # Adopting a manifest-less dir is safe ONLY when it looks like a half-created run: an
        # empty dir, or one holding only our own scaffolding. A populated foreign dir (e.g.
        # `--run ~/Documents`) is refused so run files are never scattered into the user's data.
        if self.root.exists():
            stray = [p.name for p in self.root.iterdir() if p.name not in self._RUN_DIR_ALLOWED]
            if stray:
                raise FileExistsError(
                    f"{self.root} already exists and is not a DLC run "
                    f"(no manifest.json; unexpected entries: {sorted(stray)[:5]}). Point --run at "
                    "a new or existing run directory, not an arbitrary folder.")
        self.root.mkdir(parents=True, exist_ok=True)
        for name in [
            "preflight",
            "probe_match",
            "sequences",
            "measurements",
            "generated",
            "reports",
        ]:
            (self.root / name).mkdir(exist_ok=True)

    def save(self) -> None:
        atomic_write_text(
            self.manifest_path,
            json.dumps(asdict(self.manifest), indent=2),
            encoding="utf-8",
        )

    def record_launch(self, argv: Sequence[str], *, cwd: Path | str | None = None,
                      program: str | None = None) -> dict[str, Any]:
        """Persist this CLI invocation in the manifest: the first one becomes ``launch`` (never
        overwritten), every later one is appended to ``resumes``. Saved atomically at once, so a
        run that pauses or dies at its first seam still records how it was started."""
        rec = launch_record(argv, cwd=cwd, program=program)
        if not self.manifest.launch:
            self.manifest.launch = rec
        else:
            self.manifest.resumes.append(rec)
        self.save()
        return rec

    def log(self, message: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')}  {message}"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


# ---------------------------------------------------------------------------
# runs/active.json — the advisory "which run is live, and in what state" pointer.
#
# The run that starts last owns it (the dashboard, and the DesktopLUT DLC tab, follow it to the
# next run on their own). ``state`` tracks that run: running → paused_seam (exit 10, waiting for a
# --decide resume) → running … → done / failed / cancelled. Purely advisory: every write is
# best-effort and swallowed by the callers, and a state update only lands while the pointer still
# names THIS run — a finishing older run never clobbers a newer run's pointer.
# ---------------------------------------------------------------------------

ACTIVE_POINTER_NAME = "active.json"
ACTIVE_STATES = ("running", "paused_seam", "done", "failed", "cancelled")


def active_pointer_path() -> Path:
    return runs_dir() / ACTIVE_POINTER_NAME


def _same_path(a: Any, b: Any) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, TypeError, ValueError):
        return False


def publish_active_pointer(pointer: dict[str, Any]) -> Path:
    """Write ``active.json`` (atomically) — the run that calls this takes the pointer over."""
    return atomic_write_text(active_pointer_path(), json.dumps(pointer, indent=2, default=str))


def update_active_pointer(run_root: Path | str, **fields: Any) -> bool:
    """Merge ``fields`` into ``active.json`` IF it still points at ``run_root``. Returns whether it
    wrote. Raises on I/O errors — callers treat the pointer as advisory and swallow them."""
    path = active_pointer_path()
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    owner = current.get("run") if isinstance(current, dict) else None
    if not owner or not _same_path(owner, run_root):
        return False
    current.update(fields)
    atomic_write_text(path, json.dumps(current, indent=2, default=str))
    return True


def make_run_name(mode: str, display: str | None) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = re.sub(r"[^a-z0-9._-]+", "_", display.lower()).strip("_") if display else "display"
    suffix = suffix or "display"
    return f"{timestamp}_{mode.lower()}_{suffix}"


def create_run(mode: str, display: str | None = None, run_dir: Path | None = None) -> RunContext:
    name = run_dir.name if run_dir else make_run_name(mode, display)
    # The run root MUST be absolute: paths derived from it (e.g. the generated 3D-LUT cube)
    # are sent over the IPC pipe to DesktopLUT.exe, a SEPARATE process with its own working
    # directory — a relative path would resolve against DesktopLUT's cwd and not be found.
    root = (run_dir or runs_dir() / name).resolve()
    ctx = RunContext(root=root, manifest=RunManifest(name=name, mode=mode, display=display))
    ctx.ensure_dirs()
    ctx.save()
    ctx.log("Run created")
    EventWriter(ctx.events_path).write("INFO", "init", "run_created", run=str(ctx.root))
    return ctx


def open_run(run_dir: Path) -> RunContext:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = RunManifest(
        name=raw["name"],
        mode=raw["mode"],
        display=raw.get("display"),
        created=raw.get("created", ""),
        status=raw.get("status", "created"),
        tools=raw.get("tools", {}),
        desktoplut=raw.get("desktoplut", {}),
        human_actions=raw.get("human_actions", {}),
        stages=raw.get("stages", []),
        launch=raw.get("launch") or {},
        resumes=list(raw.get("resumes") or []),
    )
    return RunContext(root=run_dir.resolve(), manifest=manifest)
