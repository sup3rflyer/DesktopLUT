"""Inspect and verify a recorded phone clip with ffprobe - the *file* is the ground truth, not the app's UI.

A camera left in the wrong mode records happily and silently (8-bit 30 fps instead of 10-bit 120 fps), so every clip
is checked against what the run asked for before anything downstream trusts it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path


def _ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise RuntimeError("ffprobe not on PATH (choco install ffmpeg)")
    return exe


@dataclass
class ClipInfo:
    path: str
    codec: str = ""
    profile: str = ""
    pix_fmt: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0              # container nominal
    frames: int = 0               # video packets actually present
    duration_s: float = 0.0
    color_transfer: str = ""
    bits: int = 0                 # from pix_fmt
    dt_ms_median: float = 0.0
    dt_ms_min: float = 0.0
    dt_ms_max: float = 0.0
    gaps: int = 0                 # inter-frame intervals > 1.5x the median (dropped / hiccup frames)
    first_gap_ms: float = 0.0     # the first interval (encoders often hiccup there - skip frame 0 in analysis)
    app_tags: dict = field(default_factory=dict)   # com.mcpro24fps.* container tags, prefix stripped
    pts: list = field(default_factory=list)        # video packet pts (s), sorted

    @property
    def measured_fps(self) -> float:
        return 1000.0 / self.dt_ms_median if self.dt_ms_median else 0.0

    def summary(self) -> str:
        return (f"{self.codec}/{self.profile} {self.pix_fmt} {self.width}x{self.height} {self.measured_fps:.2f} fps "
                f"{self.frames} frames {self.duration_s:.2f}s gaps={self.gaps}")


def probe(path: str | Path) -> ClipInfo:
    path = str(path)
    ff = _ffprobe()
    meta = json.loads(subprocess.run(
        [ff, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,profile,pix_fmt,width,height,avg_frame_rate,color_transfer:format=duration:format_tags",
         "-of", "json", path], capture_output=True, text=True, check=True).stdout)
    st = (meta.get("streams") or [{}])[0]
    pk = subprocess.run([ff, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                         "-of", "csv=p=0", path], capture_output=True, text=True, check=True).stdout.split()
    pts = sorted(float(x.split(",")[0]) for x in pk if x.strip())
    info = ClipInfo(path=path, codec=st.get("codec_name", ""), profile=st.get("profile", ""),
                    pix_fmt=st.get("pix_fmt", ""), width=int(st.get("width", 0)), height=int(st.get("height", 0)),
                    color_transfer=st.get("color_transfer", ""), frames=len(pts), pts=pts)
    try:
        info.fps = float(Fraction(st.get("avg_frame_rate", "0/1")))
    except (ZeroDivisionError, ValueError):
        info.fps = 0.0
    info.bits = 10 if "10" in info.pix_fmt else 12 if "12" in info.pix_fmt else 8 if info.pix_fmt else 0
    info.duration_s = float((meta.get("format") or {}).get("duration") or 0.0)
    info.app_tags = {k.split("com.mcpro24fps.", 1)[1]: v for k, v in (meta.get("format", {}).get("tags") or {}).items()
                     if k.startswith("com.mcpro24fps.") and "http" not in v}
    if len(pts) > 2:
        d = [(b - a) * 1000.0 for a, b in zip(pts, pts[1:])]
        ds = sorted(d)
        med = ds[len(ds) // 2]
        info.dt_ms_median, info.dt_ms_min, info.dt_ms_max = med, ds[0], ds[-1]
        info.gaps = sum(1 for x in d if x > 1.5 * med)
        info.first_gap_ms = d[0]
    return info


def check(info: ClipInfo, *, fps: float | None = None, bits: int | None = None, size: tuple[int, int] | None = None,
          codec: str | None = None, min_frames: int = 2, max_gaps: int = 0, fps_tol: float = 0.01) -> list[str]:
    """Problems with a clip vs what was asked for; empty list = good."""
    bad: list[str] = []
    if info.frames < min_frames:
        bad.append(f"only {info.frames} frames")
    if codec and info.codec != codec:
        bad.append(f"codec {info.codec} != {codec}")
    if bits and info.bits != bits:
        bad.append(f"{info.bits}-bit != {bits}-bit")
    if size and (info.width, info.height) != tuple(size):
        bad.append(f"{info.width}x{info.height} != {size[0]}x{size[1]}")
    if fps and info.measured_fps and abs(info.measured_fps - fps) > fps * fps_tol:
        bad.append(f"{info.measured_fps:.2f} fps != {fps:g} fps")
    if info.gaps > max_gaps:
        bad.append(f"{info.gaps} timestamp gap(s) (dropped frames) - max dt {info.dt_ms_max:.2f} ms vs median {info.dt_ms_median:.2f}")
    dropped = info.app_tags.get("DroppedFrames")
    if dropped and dropped.isdigit() and int(dropped) > 0:
        bad.append(f"app reports {dropped} dropped frames")
    return bad
