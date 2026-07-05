"""Run folder and manifest management."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .events import EventWriter
from .paths import RUNS_DIR, atomic_write_text


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

    def log(self, message: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')}  {message}"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


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
    root = (run_dir or RUNS_DIR / name).resolve()
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
    )
    return RunContext(root=run_dir.resolve(), manifest=manifest)
