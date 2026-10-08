"""Turn a pulled phone clip into numbers: frames on container pts, ROI time series, mean frames, fiducial geometry,
sync-edge timing and a pluggable tone (code -> linear) hook. Replaces the coordinate-era scratch scripts
(``agent_phonevid.py``, ``agent_phonegeo.py``, ``led_law_analysis.py``).

Everything streams: ffmpeg decodes into a pipe and one frame at a time is held in RAM, so a 4K or a long 1080p120 clip
is never loaded whole. Time is the **container pts** of every decoded frame (ffmpeg ``showinfo`` on the integer pts and
the stream time base, ``-copyts``), never ``frame index x 1/fps`` - long HS clips drop frames (docs/phone-capture-
framework.md, "Clip facts"). Codes are the decoder's own samples: the default ``pix_fmt="auto"`` asks ffmpeg for the
clip's native pixel format, so no range scaling or bit-depth conversion happens (10-bit HS clips -> uint16 0..1023
limited-range codes; 8-bit clips -> uint8). Frames come in **display orientation** (the clip's display matrix is
applied, as a player and the 09-xx scratch tools did - the S25 mount records 180-degree-tagged clips); pass
``autorotate=False`` for the coded (sensor read-out) orientation.

    from dlc.phone.analysis import FID2, ToneCurve, epochs, fit_fiducials, mean_frame, roi_series, sync_edges
    tone = ToneCurve.from_json("results/phone_camera_2026-09-19/video_step0/tone_response.json", bits=10)
    geo = fit_fiducials(mean_frame("fid2.mp4", 0.5, 1.0), FID2, tone=tone)    # screen px -> image px, err_px, bar_ok
    rs = roi_series("run.mp4", {"sync": geo.roi(340, 970, 560, 1190), "halo": geo.roi(2100, 1000, 2180, 1100)},
                    tone=tone)                                                 # one decode pass, per-ROI mean/std
    edges = sync_edges(rs["sync"])                                             # sub-frame half-height crossings
    E, kept = epochs(rs["halo"], [e.t for e in edges if e.kind == "rise"], np.arange(-0.05, 0.5, 1 / 240))

Coordinate conventions: screen coordinates are continuous with pixel k spanning [k, k+1) (a 120-px square at x=0 has
its centre at 60); image coordinates are numpy pixel-centre indices (pixel (row i, col j) is the point (j, i)).
ROIs are ``(x, y, w, h)`` in image pixels of the frames as decoded (after ``scale`` when one is given).

Facts only, no judgement (DLC design law): fit errors, bracketing gaps and contrast are reported for the LLM to judge.
numpy is required; scipy only for the fiducial blob finder and the homography polish.
"""

from __future__ import annotations

import collections
import dataclasses
import functools
import json
import math
import queue
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, NamedTuple, Sequence

import numpy as np

from .clip import ClipInfo, _ffprobe, probe

_EPS_S = 1e-6           # pts comparisons: container pts are exact rationals, user times are decimals


# ===================================================================================================== ffmpeg plumbing

def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:                                            # next to ffprobe (clip.py's locator) - a partial PATH install
        fp = Path(_ffprobe())
        cand = fp.with_name("ffmpeg" + fp.suffix)
        if cand.exists():
            return str(cand)
    except RuntimeError:
        pass
    raise RuntimeError("ffmpeg not on PATH (choco install ffmpeg)")


@functools.lru_cache(maxsize=None)
def _showinfo_spec(ff: str) -> str:
    """``showinfo`` without the per-frame plane checksums (pure CPU cost) when this ffmpeg has the option."""
    try:
        txt = subprocess.run([ff, "-hide_banner", "-h", "filter=showinfo"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        txt = ""
    return "showinfo=checksum=0" if "checksum" in txt else "showinfo"


@dataclass(frozen=True)
class PixFmt:
    """A planar raw-video layout ffmpeg can write to a pipe (bytes/sample, bits, chroma log2 subsampling, planes)."""
    name: str
    nbytes: int
    bits: int
    cw: int = 1
    ch: int = 1
    planes: int = 3

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(np.uint8) if self.nbytes == 1 else np.dtype("<u2")

    def plane_shapes(self, w: int, h: int) -> list[tuple[int, int]]:
        if self.planes == 1:
            return [(h, w)]
        cw, ch = -(-w // (1 << self.cw)), -(-h // (1 << self.ch))
        return [(h, w), (ch, cw), (ch, cw)]

    def frame_bytes(self, w: int, h: int) -> int:
        return sum(a * b for a, b in self.plane_shapes(w, h)) * self.nbytes


def _fmts() -> dict[str, PixFmt]:
    out = {"gray": PixFmt("gray", 1, 8, 0, 0, 1), "gray10le": PixFmt("gray10le", 2, 10, 0, 0, 1),
           "gray12le": PixFmt("gray12le", 2, 12, 0, 0, 1), "gray16le": PixFmt("gray16le", 2, 16, 0, 0, 1)}
    for sub, (cw, ch) in {"420": (1, 1), "422": (1, 0), "444": (0, 0)}.items():
        for j in ("", "j"):
            out[f"yuv{j}{sub}p"] = PixFmt(f"yuv{j}{sub}p", 1, 8, cw, ch)
        for bits in (10, 12, 16):
            out[f"yuv{sub}p{bits}le"] = PixFmt(f"yuv{sub}p{bits}le", 2, bits, cw, ch)
    return out


PIX_FMTS: dict[str, PixFmt] = _fmts()


def _resolve_pix_fmt(pix_fmt: str, info: ClipInfo) -> PixFmt:
    """``auto`` = the decoder's own format when it is a plain planar one (bit-exact, no range/depth conversion)."""
    if pix_fmt != "auto":
        if pix_fmt not in PIX_FMTS:
            raise ValueError(f"pix_fmt {pix_fmt!r} not supported; use 'auto' or one of {sorted(PIX_FMTS)}")
        return PIX_FMTS[pix_fmt]
    if info.pix_fmt in PIX_FMTS:
        return PIX_FMTS[info.pix_fmt]
    bits = info.bits or 8                         # nv12 / p010le / rgb ... -> the planar YUV of the same depth
    return PIX_FMTS["yuv420p" if bits <= 8 else f"yuv420p{min(b for b in (10, 12, 16) if b >= bits)}le"]


def _as_info(clip: "str | Path | ClipInfo") -> ClipInfo:
    return clip if isinstance(clip, ClipInfo) else probe(clip)


def clip_rotation(clip: "str | Path | ClipInfo") -> int:
    """Display rotation in degrees as ffprobe reports it (display matrix, else the legacy ``rotate`` tag); 0 = none.
    ``abs(r) % 180 == 90`` means display-oriented frames have width and height swapped."""
    path = clip.path if isinstance(clip, ClipInfo) else str(clip)
    st = Path(path).stat()
    return _rotation_cached(path, st.st_size, st.st_mtime_ns)


@functools.lru_cache(maxsize=256)
def _rotation_cached(path: str, size: int, mtime_ns: int) -> int:
    out = subprocess.run([_ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream_side_data:stream_tags=rotate", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    st = (json.loads(out or "{}").get("streams") or [{}])[0]
    for sd in st.get("side_data_list") or []:
        if "rotation" in sd:
            return int(round(float(sd["rotation"])))
    tag = (st.get("tags") or {}).get("rotate")
    return int(round(float(tag))) if tag else 0


def _display_wh(info: ClipInfo, autorotate: bool) -> tuple[int, int]:
    if autorotate and abs(clip_rotation(info)) % 180 == 90:
        return info.height, info.width
    return info.width, info.height


_TB_RE = re.compile(r"config in time_base:\s*(\d+)/(\d+)")
_FRAME_RE = re.compile(r"\bn:\s*(\d+)\s+pts:\s*(\S+)\s+pts_time:(\S+).*?\bs:(\d+)x(\d+)")
_EOF = object()


def _pump_stderr(stream, q: queue.Queue, tail: collections.deque) -> None:
    """Parse ffmpeg's stderr: one (pts_s, w, h) per showinfo frame line onto ``q``; everything else into ``tail``."""
    tb: Fraction | None = None
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if "Parsed_showinfo" not in line:
                tail.append(line)
                continue
            m = _TB_RE.search(line)
            if m:
                tb = Fraction(int(m[1]), int(m[2])) if int(m[2]) else None
                continue
            m = _FRAME_RE.search(line)
            if not m:
                continue                                   # showinfo's colour/side-data continuation lines
            pts_raw, pts_time = m[2], m[3]
            if tb is not None and re.fullmatch(r"-?\d+", pts_raw):
                t = float(int(pts_raw) * tb)               # exact: pts_time is printed with only 6 significant digits
            else:
                try:
                    t = float(pts_time)
                except ValueError:
                    t = float("nan")
            q.put((t, int(m[4]), int(m[5])))
    finally:
        q.put(_EOF)


def _decode(info: ClipInfo, fmt: PixFmt, *, seek_s: float | None, scale: tuple[int, int] | None,
            out_wh: tuple[int, int], dec_wh: tuple[int, int], autorotate: bool = True,
            extract_y: bool = False) -> Iterator[tuple[float, bytes]]:
    """Yield (container pts in s, raw frame bytes) for every decoded frame from the seek point on.
    ``dec_wh`` = the frame size the filter graph must see (display-oriented when ``autorotate``); ``extract_y``:
    pipe only the Y plane (``extractplanes`` = a plane copy; ``fmt`` is then the gray format)."""
    ff = _ffmpeg()
    vf = _showinfo_spec(ff)
    if scale:
        vf += f",scale={scale[0]}:{scale[1]}:flags=area"
    if extract_y:
        vf += ",extractplanes=y"
    cmd = [ff, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info"]
    if seek_s is not None:
        cmd += ["-ss", f"{seek_s:.6f}", "-noaccurate_seek"]   # lands on a keyframe <= target; pts filter does the rest
    cmd += ["-copyts"] + ([] if autorotate else ["-noautorotate"])
    cmd += ["-i", info.path, "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf,
            "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", fmt.name, "-"]
    nbytes = fmt.frame_bytes(*out_wh)
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=1 << 20)
    q: queue.Queue = queue.Queue()
    tail: collections.deque = collections.deque(maxlen=40)
    th = threading.Thread(target=_pump_stderr, args=(proc.stderr, q, tail), daemon=True)
    th.start()

    def fail(msg: str) -> RuntimeError:
        return RuntimeError(f"{msg} [{Path(info.path).name}]; ffmpeg said: " + " | ".join(list(tail)[-6:]))

    n = 0
    try:
        while True:
            buf = proc.stdout.read(nbytes)
            if not buf:
                break
            if len(buf) < nbytes:
                raise fail(f"truncated frame {n}: {len(buf)} of {nbytes} bytes")
            try:
                item = q.get(timeout=60)
            except queue.Empty:
                raise fail(f"no showinfo pts for frame {n} within 60 s (ffmpeg log format changed?)") from None
            if item is _EOF:
                raise fail(f"frame {n} has no showinfo pts line")
            t, w, h = item
            if (w, h) != tuple(dec_wh):
                raise fail(f"frame {n} decoded at {w}x{h}, expected {dec_wh[0]}x{dec_wh[1]} (size change?)")
            if not math.isfinite(t):
                raise fail(f"frame {n} has no pts - refusing to invent one from the frame index")
            n += 1
            yield t, buf
        rc = proc.wait()
        th.join(timeout=10)
        if rc != 0:
            raise fail(f"ffmpeg exited {rc}")
        left = []
        while not q.empty():
            it = q.get_nowait()
            if it is not _EOF:
                left.append(it)
        if left:
            raise fail(f"{len(left)} showinfo pts without a frame on the pipe")
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.stdout.close()
        finally:
            proc.wait()
            th.join(timeout=10)
            if not th.is_alive():
                proc.stderr.close()


def _check_scale(scale) -> tuple[int, int] | None:
    if scale is None:
        return None
    try:
        w, h = (int(v) for v in scale)
    except (TypeError, ValueError):
        raise ValueError(f"scale must be (width, height) in pixels, got {scale!r}") from None
    if w <= 0 or h <= 0:
        raise ValueError(f"scale must be positive, got {scale!r}")
    return w, h


def _planes(buf: bytes, fmt: PixFmt, w: int, h: int) -> list[np.ndarray]:
    out, off = [], 0
    for ph, pw in fmt.plane_shapes(w, h):
        n = ph * pw
        out.append(np.frombuffer(buf, dtype=fmt.dtype, count=n, offset=off).reshape(ph, pw))
        off += n * fmt.nbytes
    return out


def iter_frames(clip: "str | Path | ClipInfo", *, plane: str = "y", pix_fmt: str = "auto",
                start_s: float | None = None, dur_s: float | None = None,
                scale: tuple[int, int] | None = None, autorotate: bool = True) -> Iterator[tuple[float, Any]]:
    """Stream ``(pts_s, frame)`` in presentation order; one frame in RAM at a time.

    ``pts_s`` is the container pts in seconds (raw, ``-copyts``: the same timeline as ``clip.probe().pts``).
    ``plane``: ``"y"`` / ``"u"`` / ``"v"`` -> one read-only ndarray (uint8 for 8-bit formats, uint16 otherwise);
    ``"yuv"`` -> a tuple of the three planes. ``pix_fmt="auto"`` = the clip's native format (bit-exact codes); any
    key of :data:`PIX_FMTS` forces a conversion by ffmpeg (e.g. ``"yuv420p10le"`` - an 8-bit source then comes out
    shifted to 10-bit codes). ``start_s`` / ``dur_s`` select the half-open pts window ``[start_s, start_s + dur_s)``
    (``dur_s`` alone counts from the first frame). ``scale=(w, h)`` resizes with area averaging (after rotation).
    ``autorotate`` (default) applies the clip's display matrix - a pixel permutation, codes unchanged - so frames
    look as in a player (a 90-degree tag swaps width/height); ``False`` = coded orientation (sensor read-out rows).
    """
    info = _as_info(clip)
    if plane not in ("y", "u", "v", "yuv"):
        raise ValueError(f"plane must be 'y', 'u', 'v' or 'yuv', got {plane!r}")
    fmt = _resolve_pix_fmt(pix_fmt, info)
    if fmt.planes == 1 and plane != "y":
        raise ValueError(f"{fmt.name} has only a Y plane")
    sc = _check_scale(scale)
    if not (info.width and info.height):
        raise ValueError(f"{info.path}: no video size from ffprobe")
    dec_wh = _display_wh(info, autorotate)
    w, h = sc or dec_wh
    if dur_s is not None and dur_s <= 0:
        raise ValueError(f"dur_s must be > 0, got {dur_s}")
    return _iter_frames(info, fmt, plane, start_s, dur_s, sc, (w, h), dec_wh, autorotate)


def _iter_frames(info, fmt, plane, start_s, dur_s, sc, wh, dec_wh, autorotate) -> Iterator[tuple[float, Any]]:
    w, h = wh
    first_pts = info.pts[0] if info.pts else 0.0
    seek = None
    if start_s is not None and start_s - first_pts > 1.0:
        seek = start_s - first_pts - 0.5           # ffmpeg's -ss is relative to the file start; back off half a second
    pidx = {"y": 0, "u": 1, "v": 2}.get(plane)
    # Y only, native format, no resize: pipe just the Y plane (a third less data). extractplanes copies the plane;
    # it is not used when ffmpeg would have to convert first (forced pix_fmt, scale, full-range yuvj*).
    extract = plane == "y" and fmt.planes == 3 and fmt.name == info.pix_fmt and "j" not in fmt.name and sc is None
    if extract:
        fmt = PIX_FMTS[{8: "gray", 10: "gray10le", 12: "gray12le", 16: "gray16le"}[fmt.bits]]
    for attempt in (0, 1):
        lo = start_s
        hi = None if dur_s is None or start_s is None else start_s + dur_s
        first = True
        restart = False
        gen = _decode(info, fmt, seek_s=seek if attempt == 0 else None, scale=sc, out_wh=(w, h), dec_wh=dec_wh,
                      autorotate=autorotate, extract_y=extract)
        try:
            for t, buf in gen:
                if first:
                    first = False
                    if lo is None:
                        lo = t
                        hi = None if dur_s is None else t + dur_s
                    elif seek is not None and attempt == 0 and t > lo + _EPS_S:
                        # the seek may have overshot the window start (keyframe placement, odd start_time): never
                        # guess - decode again from the top
                        expected = next((p for p in info.pts if p >= lo - _EPS_S), None)
                        if expected is None or t > expected + 1e-5:
                            restart = True
                            break
                if t < lo - _EPS_S:
                    continue
                if hi is not None and t >= hi - _EPS_S:
                    return
                planes = _planes(buf, fmt, w, h)
                yield t, (tuple(planes) if pidx is None else planes[pidx])
        finally:
            gen.close()                             # kills ffmpeg when the caller stops early
        if not restart:
            return


# ======================================================================================================== tone curves

@dataclass(frozen=True, eq=False)
class ToneCurve:
    """Camera code -> linear signal (units of whatever the curve was characterised against). Pluggable kinds:

    * ``identity`` - lin = code (the default: analysis in codes).
    * ``log10``    - code = a*log10(b*L + c) + d (the 2026-09-19 S25 LOG fit; loads from its tone_response.json).
    * ``power``    - L = scale * sign(x)*|x|**gamma, x = (code - black) / (white - black).
    * ``table``    - piecewise-linear through measured (codes, lin) points, linear extrapolation past the ends.
    * ``function`` - any Python callable (``from_function``); not serialisable.

    ``bits`` = the code domain the curve was fit in; the readers refuse a curve whose ``bits`` differs from the
    frames' bit depth. ``sat_code`` marks clipping (``saturated()``) - linear values at/above it are not physical.
    """
    kind: str = "identity"
    params: Mapping[str, Any] = field(default_factory=dict)
    name: str = "identity"
    bits: int | None = None
    sat_code: float | None = None
    units: str = "code"
    meta: Mapping[str, Any] = field(default_factory=dict)
    fn: Callable[[np.ndarray], np.ndarray] | None = field(default=None, repr=False)
    inv_fn: Callable[[np.ndarray], np.ndarray] | None = field(default=None, repr=False)

    def __post_init__(self):
        p = self.params
        need = {"identity": (), "log10": ("a", "b", "c", "d"), "power": ("black", "white", "gamma"),
                "table": ("codes", "lin"), "function": ()}
        if self.kind not in need:
            raise ValueError(f"unknown tone kind {self.kind!r}; one of {sorted(need)}")
        miss = [k for k in need[self.kind] if k not in p]
        if miss:
            raise ValueError(f"tone kind {self.kind!r} needs params {miss}")
        if self.kind == "function" and not callable(self.fn):
            raise ValueError("kind 'function' needs fn")
        if self.kind == "table":
            c, v = np.asarray(p["codes"], float), np.asarray(p["lin"], float)
            if c.ndim != 1 or c.shape != v.shape or c.size < 2 or np.any(np.diff(c) <= 0):
                raise ValueError("table tone needs >= 2 strictly increasing codes and the same number of lin values")
        if self.kind == "power" and float(p["white"]) == float(p["black"]):
            raise ValueError("power tone needs white != black")

    # --- constructors ---
    @classmethod
    def identity(cls, bits: int | None = None) -> "ToneCurve":
        return cls(bits=bits)

    @classmethod
    def log10(cls, a: float, b: float, c: float, d: float, **kw) -> "ToneCurve":
        return cls(kind="log10", params={"a": a, "b": b, "c": c, "d": d}, **{"name": "log10", **kw})

    @classmethod
    def power(cls, black: float, white: float, gamma: float, scale: float = 1.0, **kw) -> "ToneCurve":
        return cls(kind="power", params={"black": black, "white": white, "gamma": gamma, "scale": scale},
                   **{"name": "power", **kw})

    @classmethod
    def table(cls, codes: Sequence[float], lin: Sequence[float], **kw) -> "ToneCurve":
        return cls(kind="table", params={"codes": [float(x) for x in codes], "lin": [float(x) for x in lin]},
                   **{"name": "table", **kw})

    @classmethod
    def from_function(cls, fn: Callable, name: str, inverse: Callable | None = None, **kw) -> "ToneCurve":
        return cls(kind="function", fn=fn, inv_fn=inverse, name=name, **kw)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], *, bits: int | None = None) -> "ToneCurve":
        """Current schema ``{"kind", "params", "name", "bits", "sat_code", "units", "meta"}``, or the legacy
        2026-09-19 ``tone_response.json`` shape (``{"fit": {"form": "code = a*log10(b*L + c) + d", a, b, c, d}}``).
        ``bits`` overrides/sets the code domain (the legacy file does not record it)."""
        if "kind" in d:
            return cls(kind=d["kind"], params=dict(d.get("params") or {}), name=d.get("name") or d["kind"],
                       bits=bits if bits is not None else d.get("bits"), sat_code=d.get("sat_code"),
                       units=d.get("units", "code" if d["kind"] == "identity" else "linear"),
                       meta=dict(d.get("meta") or {}))
        fit = d.get("fit")
        if isinstance(fit, Mapping) and all(k in fit for k in "abcd"):
            form = str(fit.get("form", "log10"))
            if "log10" not in form.replace(" ", ""):
                raise ValueError(f"legacy tone fit form not understood: {form!r}")
            video = d.get("video") or {}
            name = "s25-" + str(video.get("mode", "log")).lower().replace(" ", "-") if video else "legacy-log10"
            return cls.log10(*(float(fit[k]) for k in "abcd"), name=name, bits=bits, sat_code=d.get("sat_code"),
                             units=form.rsplit("(", 1)[-1].rstrip(")") if " (" in form else "linear",
                             meta={"source": "legacy tone_response.json", "form": form, "video": dict(video),
                                   "resid_lin_rms_pct": d.get("resid_lin_rms_pct")})
        raise ValueError("not a tone curve: needs 'kind' or a legacy 'fit' with a, b, c, d")

    @classmethod
    def from_json(cls, path: str | Path, *, bits: int | None = None) -> "ToneCurve":
        tc = cls.from_dict(json.loads(Path(path).read_text()), bits=bits)
        return dataclasses.replace(tc, meta={**tc.meta, "path": str(path)})

    def to_dict(self) -> dict:
        if self.kind == "function":
            raise ValueError("a 'function' tone curve cannot be serialised")
        return {"kind": self.kind, "name": self.name, "bits": self.bits, "sat_code": self.sat_code,
                "units": self.units, "params": dict(self.params), "meta": dict(self.meta)}

    def to_json(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=1, default=float))
        return path

    # --- evaluation ---
    @property
    def is_identity(self) -> bool:
        return self.kind == "identity"

    def __call__(self, code):
        x = np.asarray(code, dtype=np.float64)
        p = self.params
        if self.kind == "identity":
            out = x
        elif self.kind == "log10":
            out = (10.0 ** ((x - p["d"]) / p["a"]) - p["c"]) / p["b"]
        elif self.kind == "power":
            v = (x - p["black"]) / (p["white"] - p["black"])
            out = p.get("scale", 1.0) * np.sign(v) * np.abs(v) ** p["gamma"]
        elif self.kind == "table":
            out = _interp_extrap(x, np.asarray(p["codes"], float), np.asarray(p["lin"], float))
        else:
            out = np.asarray(self.fn(x), dtype=np.float64)
        return out if out.ndim else float(out)

    def inverse(self, lin):
        y = np.asarray(lin, dtype=np.float64)
        p = self.params
        if self.kind == "identity":
            out = y
        elif self.kind == "log10":
            out = p["a"] * np.log10(p["b"] * y + p["c"]) + p["d"]
        elif self.kind == "power":
            v = y / p.get("scale", 1.0)
            out = p["black"] + (p["white"] - p["black"]) * np.sign(v) * np.abs(v) ** (1.0 / p["gamma"])
        elif self.kind == "table":
            c, v = np.asarray(p["codes"], float), np.asarray(p["lin"], float)
            if np.all(np.diff(v) > 0):
                out = _interp_extrap(y, v, c)
            elif np.all(np.diff(v) < 0):
                out = _interp_extrap(y, v[::-1], c[::-1])
            else:
                raise ValueError("table tone is not monotonic - no inverse")
        else:
            if self.inv_fn is None:
                raise ValueError(f"tone {self.name!r} has no inverse")
            out = np.asarray(self.inv_fn(y), dtype=np.float64)
        return out if out.ndim else float(out)

    def saturated(self, code) -> np.ndarray:
        x = np.asarray(code, dtype=np.float64)
        return x >= self.sat_code if self.sat_code is not None else np.zeros(x.shape, bool)

    def check_bits(self, bits: int) -> None:
        if self.bits is not None and bits and int(self.bits) != int(bits):
            raise ValueError(f"tone curve {self.name!r} is for {self.bits}-bit codes, the frames are {bits}-bit")


def _interp_extrap(x: np.ndarray, xp: np.ndarray, fp: np.ndarray) -> np.ndarray:
    out = np.interp(x, xp, fp)
    lo, hi = x < xp[0], x > xp[-1]
    if lo.any():
        out = np.where(lo, fp[0] + (x - xp[0]) * (fp[1] - fp[0]) / (xp[1] - xp[0]), out)
    if hi.any():
        out = np.where(hi, fp[-1] + (x - xp[-1]) * (fp[-1] - fp[-2]) / (xp[-1] - xp[-2]), out)
    return out


IDENTITY = ToneCurve()


def _tone_for(tone: ToneCurve | None, fmt: PixFmt) -> ToneCurve | None:
    if tone is None or tone.is_identity:
        return None
    tone.check_bits(fmt.bits)
    return tone


# ===================================================================================================== ROI time series

class Series(NamedTuple):
    """One time series on container pts: ``t`` (s) and ``y`` (same length)."""
    t: np.ndarray
    y: np.ndarray


@dataclass
class RoiSeries:
    """Per-frame ROI statistics from one decode pass. ``rs["sync"]`` -> :class:`Series` of that ROI's mean."""
    t: np.ndarray
    mean: dict[str, np.ndarray]
    std: dict[str, np.ndarray]
    n_px: dict[str, int]
    rois: dict[str, tuple[int, int, int, int]]
    clip: str = ""
    pix_fmt: str = ""
    bits: int = 0
    tone: str = "identity"

    def __getitem__(self, name: str) -> Series:
        return Series(self.t, self.mean[name])

    def __len__(self) -> int:
        return int(self.t.size)

    def as_dict(self) -> dict[str, np.ndarray]:
        """The old ``agent_phonevid.trace`` layout: ``{'t', name, name + '_sd'}``."""
        out = {"t": self.t}
        for k in self.mean:
            out[k] = self.mean[k]
            if k in self.std:
                out[k + "_sd"] = self.std[k]
        return out

    def save_npz(self, path: str | Path) -> Path:
        path = Path(path)
        np.savez(path, **self.as_dict(), _meta=np.array(json.dumps(
            {"clip": self.clip, "pix_fmt": self.pix_fmt, "bits": self.bits, "tone": self.tone, "n_px": self.n_px,
             "rois": self.rois})))
        return path


def _check_rois(rois: Mapping[str, Sequence[float]], w: int, h: int,
                masks: Mapping[str, np.ndarray] | None) -> tuple[dict, dict]:
    if not rois:
        raise ValueError("no ROIs")
    out, mk = {}, {}
    for name, r in rois.items():
        try:
            vals = [float(v) for v in r]
        except (TypeError, ValueError):
            raise ValueError(f"ROI {name!r}: expected (x, y, w, h), got {r!r}") from None
        if len(vals) != 4 or any(v != int(v) for v in vals):
            raise ValueError(f"ROI {name!r}: expected 4 integer pixels (x, y, w, h), got {r!r}")
        x, y, rw, rh = (int(v) for v in vals)
        if rw <= 0 or rh <= 0 or x < 0 or y < 0 or x + rw > w or y + rh > h:
            raise ValueError(f"ROI {name!r} {(x, y, rw, rh)} is empty or outside the {w}x{h} frame")
        out[name] = (x, y, rw, rh)
    for name, m in (masks or {}).items():
        if name not in out:
            raise ValueError(f"mask for unknown ROI {name!r}")
        m = np.asarray(m, dtype=bool)
        if m.shape != (out[name][3], out[name][2]):
            raise ValueError(f"mask {name!r} has shape {m.shape}, the ROI is {(out[name][3], out[name][2])} (h, w)")
        if not m.any():
            raise ValueError(f"mask {name!r} selects no pixels")
        mk[name] = m
    return out, mk


def roi_series(clip: "str | Path | ClipInfo", rois: Mapping[str, Sequence[int]], *,
               masks: Mapping[str, np.ndarray] | None = None, start_s: float | None = None,
               dur_s: float | None = None, plane: str = "y", pix_fmt: str = "auto",
               scale: tuple[int, int] | None = None, tone: ToneCurve | None = None,
               std: bool = True, autorotate: bool = True) -> RoiSeries:
    """Per-frame mean (and spatial std) of every ROI ``{name: (x, y, w, h)}`` in ONE decode pass.

    ``masks[name]`` (bool, the ROI's (h, w)) restricts that ROI to the True pixels. ``tone`` linearises every pixel
    before averaging (the mean of linear light, not the linearised mean code). Times are container pts.
    """
    info = _as_info(clip)
    fmt = _resolve_pix_fmt(pix_fmt, info)
    sc = _check_scale(scale)
    w, h = sc or _display_wh(info, autorotate)
    if plane in ("u", "v"):
        h, w = fmt.plane_shapes(w, h)[1]
    elif plane != "y":
        raise ValueError(f"roi_series reads one plane ('y', 'u' or 'v'), got {plane!r}")
    R, M = _check_rois(rois, w, h, masks)
    tc = _tone_for(tone, fmt)
    ts: list[float] = []
    mu: dict[str, list[float]] = {k: [] for k in R}
    sd: dict[str, list[float]] = {k: [] for k in R}
    for t, fr in iter_frames(info, plane=plane, pix_fmt=fmt.name, start_s=start_s, dur_s=dur_s, scale=sc,
                             autorotate=autorotate):
        ts.append(t)
        for k, (x, y, rw, rh) in R.items():
            c = fr[y:y + rh, x:x + rw]
            c = tc(c) if tc is not None else c.astype(np.float64)
            if k in M:
                c = c[M[k]]
            mu[k].append(float(c.mean()))
            if std:
                sd[k].append(float(c.std()))
    return RoiSeries(t=np.asarray(ts, dtype=np.float64), mean={k: np.asarray(v) for k, v in mu.items()},
                     std={k: np.asarray(v) for k, v in sd.items()} if std else {},
                     n_px={k: int(M[k].sum()) if k in M else R[k][2] * R[k][3] for k in R}, rois=R,
                     clip=info.path, pix_fmt=fmt.name, bits=fmt.bits, tone=tc.name if tc else "identity")


def mean_frame(clip: "str | Path | ClipInfo", start_s: float | None = None, dur_s: float | None = None, *,
               plane: str = "y", pix_fmt: str = "auto", scale: tuple[int, int] | None = None,
               tone: ToneCurve | None = None, return_n: bool = False, autorotate: bool = True):
    """Time-averaged frame (float64) over the pts window - geometry / flat fields. ``tone`` averages linear light."""
    info = _as_info(clip)
    fmt = _resolve_pix_fmt(pix_fmt, info)
    if plane not in ("y", "u", "v"):
        raise ValueError(f"mean_frame averages one plane ('y', 'u' or 'v'), got {plane!r}")
    tc = _tone_for(tone, fmt)
    acc, n = None, 0
    for _, fr in iter_frames(info, plane=plane, pix_fmt=fmt.name, start_s=start_s, dur_s=dur_s, scale=scale,
                             autorotate=autorotate):
        f = tc(fr) if tc is not None else fr
        if acc is None:
            acc = np.zeros(fr.shape, dtype=np.float64)
        acc += f
        n += 1
    if not n:
        raise ValueError(f"no frames in the window start_s={start_s} dur_s={dur_s} of {info.path}")
    m = acc / n
    return (m, n) if return_n else m


# ========================================================================================================== geometry

Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class FiducialLayout:
    """Bright marks on a dark screen, screen px rects ``(x, y, w, h)``. ``bar`` is the orientation-breaking mark: it
    is fitted like the others, and a 180-degree-rotated hypothesis cannot match it."""
    name: str
    screen: tuple[int, int]
    marks: tuple[Rect, ...]
    bar: Rect | None = None

    def rects(self) -> list[Rect]:
        return list(self.marks) + ([self.bar] if self.bar is not None else [])

    def points(self) -> np.ndarray:
        """Mark centres (screen px), the bar last."""
        return np.array([(x + w / 2.0, y + h / 2.0) for x, y, w, h in self.rects()], dtype=np.float64)

    def areas(self) -> np.ndarray:
        return np.array([w * h for _, _, w, h in self.rects()], dtype=np.float64)

    @property
    def bar_index(self) -> int | None:
        return len(self.marks) if self.bar is not None else None


#: The 2026-09-19..22 "fid2" frame (agent_p11_session.FID2 on a 3840x2160 panel): 120-px squares at the four corners
#: and the centre, 60-px squares at (930|2910, 510|1650) centres, and a 240x60 bar at (400, 200).
FID2 = FiducialLayout(
    "fid2", (3840, 2160),
    marks=((0, 0, 120, 120), (3720, 0, 120, 120), (0, 2040, 120, 120), (3720, 2040, 120, 120), (1860, 1020, 120, 120),
           (900, 480, 60, 60), (2880, 480, 60, 60), (900, 1620, 60, 60), (2880, 1620, 60, 60)),
    bar=(400, 200, 240, 60))


class FiducialError(RuntimeError):
    pass


def apply_h(H, pts) -> np.ndarray:
    """Map (n, 2) points through the 3x3 homography ``H``."""
    p = np.atleast_2d(np.asarray(pts, dtype=np.float64))
    q = np.c_[p, np.ones(len(p))] @ np.asarray(H, dtype=np.float64).T
    return q[:, :2] / q[:, 2:3]


def _norm_t(p: np.ndarray) -> np.ndarray:
    c = p.mean(0)
    d = np.sqrt(((p - c) ** 2).sum(1)).mean()
    s = math.sqrt(2.0) / d if d > 0 else 1.0
    return np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1.0]])


def homography(src, dst) -> np.ndarray:
    """Normalised DLT homography mapping ``src`` -> ``dst`` (n >= 4 point pairs), ``H[2, 2] == 1``."""
    src, dst = np.asarray(src, dtype=np.float64), np.asarray(dst, dtype=np.float64)
    if len(src) < 4 or len(src) != len(dst):
        raise ValueError(f"homography needs >= 4 point pairs, got {len(src)}/{len(dst)}")
    ts, td = _norm_t(src), _norm_t(dst)
    s, d = apply_h(ts, src), apply_h(td, dst)
    A = []
    for (x, y), (u, v) in zip(s, d):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    _, _, vt = np.linalg.svd(np.asarray(A))
    H = np.linalg.inv(td) @ vt[-1].reshape(3, 3) @ ts
    return H / H[2, 2]


def _polish(H: np.ndarray, src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares on the geometric (image px) reprojection error, from the DLT start."""
    if len(src) < 5:
        return H
    from scipy.optimize import least_squares
    r = least_squares(lambda h: (apply_h(np.append(h, 1.0).reshape(3, 3), src) - dst).ravel(),
                      (H / H[2, 2]).ravel()[:8], method="lm")
    H2 = np.append(r.x, 1.0).reshape(3, 3)
    e0 = np.abs(apply_h(H, src) - dst).sum()
    return H2 if np.isfinite(H2).all() and np.abs(apply_h(H2, src) - dst).sum() <= e0 else H


def roi(H, sx0: float, sy0: float, sx1: float, sy1: float) -> tuple[int, int, int, int]:
    """Screen rect (corners, screen px) -> the axis-aligned image ROI ``(x, y, w, h)`` inside its warped image."""
    p = apply_h(H, [(sx0, sy0), (sx1, sy0), (sx0, sy1), (sx1, sy1)])
    xs, ys = np.sort(p[:, 0]), np.sort(p[:, 1])
    x0, y0, x1, y1 = int(math.ceil(xs[1])), int(math.ceil(ys[1])), int(math.floor(xs[2])), int(math.floor(ys[2]))
    return (x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))


def find_blobs(img, *, min_px: float | None = None, blur: float | None = None, ratio: float = 1.5,
               percentile: float = 60.0, drop_border: bool = True) -> np.ndarray:
    """Bright compact blobs: ``img > ratio x gaussian(img, blur)`` and above the ``percentile`` level.

    Returns ``(n, 3)`` = sub-pixel centroid x, y (background-subtracted intensity weights over the blob grown by
    2 px, floor = the median of a ring around it) and area (px). Blobs touching the frame edge are dropped by default
    (their centroid is biased). ``min_px`` / ``blur`` default to the 1080p values (150 px, 60 px) scaled to the image.
    """
    from scipy import ndimage
    a = np.asarray(img, dtype=np.float64)
    if a.ndim != 2:
        raise ValueError("find_blobs needs a 2-D image")
    hh, ww = a.shape
    if min_px is None:
        min_px = max(12.0, 150.0 * hh * ww / (1920.0 * 1080.0))
    if blur is None:
        blur = max(3.0, 60.0 * ww / 1920.0)
    bg = ndimage.gaussian_filter(a, blur)
    lab, n = ndimage.label((a > ratio * bg) & (a > np.percentile(a, percentile)))
    if not n:
        return np.zeros((0, 3))
    sizes = ndimage.sum_labels(np.ones_like(a), lab, np.arange(1, n + 1))
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None or sizes[i - 1] < min_px:
            continue
        ys, xs = sl
        if drop_border and (ys.start == 0 or xs.start == 0 or ys.stop == hh or xs.stop == ww):
            continue
        pad = 5
        y0, y1, x0, x1 = max(ys.start - pad, 0), min(ys.stop + pad, hh), max(xs.start - pad, 0), min(xs.stop + pad, ww)
        sub = a[y0:y1, x0:x1]
        m = lab[y0:y1, x0:x1] == i
        m2 = ndimage.binary_dilation(m, iterations=2)
        ring = ndimage.binary_dilation(m2, iterations=2) & ~m2 & (lab[y0:y1, x0:x1] == 0)
        floor = float(np.median(sub[ring])) if ring.any() else float(np.percentile(a, percentile))
        wts = np.clip(sub - floor, 0.0, None) * m2
        tot = wts.sum()
        if tot <= 0:
            cy, cx = ndimage.center_of_mass(m)
        else:
            cy, cx = ndimage.center_of_mass(wts)
        out.append((cx + x0, cy + y0, float(sizes[i - 1])))
    return np.asarray(out, dtype=np.float64).reshape(-1, 3)


@dataclass
class FiducialFit:
    """Screen px -> image px homography + how well it fits. ``err_px`` = max fiducial residual (px), ``bar_ok`` =
    the asymmetric bar was found where H puts it (``None``: no bar in the layout, or H puts it off the image - then
    the orientation rests on the ``rotations`` prior), ``rotated_180`` = the camera sees the screen upside down (H
    already accounts for it)."""
    H: np.ndarray
    n: int
    n_total: int
    err_px: float
    rms_px: float
    bar_ok: bool | None
    rotated_180: bool
    rotation_deg: float
    scale: float
    used: list[int]
    residuals_px: list[float]
    shape: tuple[int, int]
    layout: str = ""
    n_blobs: int = 0

    def apply(self, pts) -> np.ndarray:
        return apply_h(self.H, pts)

    def roi(self, sx0: float, sy0: float, sx1: float, sy1: float) -> tuple[int, int, int, int]:
        return roi(self.H, sx0, sy0, sx1, sy1)

    def to_dict(self) -> dict:
        return {"layout": self.layout, "H": np.asarray(self.H).tolist(), "n": self.n, "n_total": self.n_total,
                "err_px": self.err_px, "rms_px": self.rms_px, "bar_ok": self.bar_ok, "rotated_180": self.rotated_180,
                "rotation_deg": self.rotation_deg, "scale": self.scale, "used": self.used,
                "residuals_px": self.residuals_px, "shape": list(self.shape), "n_blobs": self.n_blobs}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "FiducialFit":
        """Also reads the scratch ``agent_phonegeo.py fit`` JSON (``H, n, err_px, bar_ok, shape``)."""
        H = np.asarray(d["H"], dtype=np.float64)
        rot, sc = _rotation_scale(H, (0.0, 0.0))
        return cls(H=H, n=int(d.get("n", 0)), n_total=int(d.get("n_total", d.get("n", 0))),
                   err_px=float(d.get("err_px", float("nan"))), rms_px=float(d.get("rms_px", float("nan"))),
                   bar_ok=d.get("bar_ok"), rotated_180=bool(d.get("rotated_180", abs(rot) > 90.0)),
                   rotation_deg=float(d.get("rotation_deg", rot)), scale=float(d.get("scale", sc)),
                   used=list(d.get("used", [])), residuals_px=list(d.get("residuals_px", [])),
                   shape=tuple(d.get("shape", (0, 0))), layout=str(d.get("layout", d.get("kind", ""))),
                   n_blobs=int(d.get("n_blobs", 0)))


def _rotation_scale(H: np.ndarray, at: tuple[float, float]) -> tuple[float, float]:
    p = apply_h(H, [at, (at[0] + 1.0, at[1])])
    v = p[1] - p[0]
    return math.degrees(math.atan2(v[1], v[0])), float(np.hypot(*v))


def _match(pred: np.ndarray, blobs: np.ndarray, tol: float, area_pred: np.ndarray | None = None
           ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Unique nearest-blob assignment within ``tol`` (closest pair wins a contested blob)."""
    if not len(blobs):
        return np.zeros(0, int), np.zeros(0, int), np.zeros(0)
    d = np.hypot(pred[:, None, 0] - blobs[None, :, 0], pred[:, None, 1] - blobs[None, :, 1])
    if area_pred is not None:
        r = blobs[None, :, 2] / np.maximum(area_pred[:, None], 1e-9)
        d = np.where((r > 0.25) & (r < 4.0), d, np.inf)
    li, bi, dd = [], [], []
    used_l, used_b = set(), set()
    for k in np.argsort(d, axis=None):
        i, j = divmod(int(k), d.shape[1])
        if d[i, j] >= tol:
            break
        if i in used_l or j in used_b:
            continue
        used_l.add(i); used_b.add(j)
        li.append(i); bi.append(j); dd.append(d[i, j])
    return np.asarray(li, int), np.asarray(bi, int), np.asarray(dd)


def fit_fiducials(img, layout: FiducialLayout = FID2, *, tone: ToneCurve | None = None,
                  min_px: float | None = None, blur: float | None = None, ratio: float = 1.5,
                  rotations: Sequence[float] | None = (0.0, 180.0), rot_tol_deg: float = 30.0,
                  refine_tol_px: float | None = None) -> FiducialFit:
    """Fit the screen->image homography from a (mean) frame of a fiducial layout.

    Seeds: every similarity transform that takes a pair of the layout's largest marks onto a pair of the largest
    blobs (rotation within ``rot_tol_deg`` of one of ``rotations``; ``None`` = any) is scored by how many marks land
    on a blob of plausible area; ties prefer the earlier entry of ``rotations`` (upright). The winner is refined to a
    homography (normalised DLT, two re-match rounds, geometric least-squares polish). The bar breaks the 0/180
    symmetry of a point-symmetric layout: the upside-down hypothesis cannot match it, so H comes out right either way.
    ``tone`` linearises the frame first (a LOG clip compresses a 40-nit square to < 1.5x a 5-nit field).
    """
    a = np.asarray(img, dtype=np.float64)
    if tone is not None and not tone.is_identity:
        a = np.asarray(tone(a), dtype=np.float64)
    blobs = find_blobs(a, min_px=min_px, blur=blur, ratio=ratio)
    P, A = layout.points(), layout.areas()
    if len(P) < 4:
        raise FiducialError("a layout needs >= 4 marks")
    if len(blobs) < 4:
        raise FiducialError(f"only {len(blobs)} blobs found (need >= 4) - wrong frame, focus, exposure or tone?")
    dP = np.hypot(*(P[:, None, :] - P[None, :, :]).transpose(2, 0, 1))
    spacing = float(dP[dP > 0].min())
    big_l = np.argsort(-A, kind="stable")[:6]
    big_b = np.argsort(-blobs[:, 2], kind="stable")[:10]
    rots = None if rotations is None else [float(r) for r in rotations]
    best_key, best = None, None
    for la in big_l:
        for lb in big_l:
            if la == lb:
                continue
            vL = P[lb] - P[la]
            nL = math.hypot(*vL)
            for ba in big_b:
                for bb in big_b:
                    if ba == bb:
                        continue
                    vB = blobs[bb, :2] - blobs[ba, :2]
                    s = math.hypot(*vB) / nL
                    if s <= 0:
                        continue
                    if not (0.25 < blobs[ba, 2] / (s * s * A[la]) < 4 and 0.25 < blobs[bb, 2] / (s * s * A[lb]) < 4):
                        continue
                    th = math.degrees(math.atan2(vB[1], vB[0]) - math.atan2(vL[1], vL[0]))
                    rank = 0
                    if rots is not None:
                        dists = [abs((th - r + 180.0) % 360.0 - 180.0) for r in rots]
                        rank = int(np.argmin(dists))
                        if dists[rank] > rot_tol_deg:
                            continue
                    c, sn = math.cos(math.radians(th)), math.sin(math.radians(th))
                    pred = (P - P[la]) @ (s * np.array([[c, -sn], [sn, c]])).T + blobs[ba, :2]
                    li, bi, dd = _match(pred, blobs, 0.2 * spacing * s, s * s * A)
                    key = (len(li), -rank, -float(np.sqrt(np.mean(dd ** 2))) / s if len(dd) else 0.0)
                    if best_key is None or key > best_key:
                        best_key, best = key, (pred, s)
    if best is None or best_key[0] < 4:
        raise FiducialError(f"no consistent layout match ({len(blobs)} blobs, best {best_key and best_key[0]} marks)"
                            " - is the fiducial frame on screen and the layout right?")
    pred, s = best
    li, bi, _ = _match(pred, blobs, 0.2 * spacing * s, s * s * A)
    H = homography(P[li], blobs[bi, :2])
    rtol = refine_tol_px if refine_tol_px is not None else max(3.0, 0.08 * spacing * s)
    for tol in (0.2 * spacing * s, rtol):
        li, bi, _ = _match(apply_h(H, P), blobs, tol)
        if len(li) < 4:
            raise FiducialError(f"only {len(li)} marks within {tol:.1f} px after refinement")
        H = homography(P[li], blobs[bi, :2])
    H = _polish(H, P[li], blobs[bi, :2])
    order = np.argsort(li)
    li, bi = li[order], bi[order]
    res = np.hypot(*(apply_h(H, P[li]) - blobs[bi, :2]).T)
    rot, sc = _rotation_scale(H, (layout.screen[0] / 2.0, layout.screen[1] / 2.0))
    bar_i, bar_ok = layout.bar_index, None
    if bar_i is not None:
        bx, by = apply_h(H, P[bar_i:bar_i + 1])[0]
        m = 0.5 * math.sqrt(A[bar_i]) * sc                          # bar off-image -> unverifiable (None), not False
        on_image = m <= bx < a.shape[1] - m and m <= by < a.shape[0] - m
        bar_ok = True if bar_i in set(li.tolist()) else (False if on_image else None)
    return FiducialFit(H=H, n=int(len(li)), n_total=len(P), err_px=float(res.max()),
                       rms_px=float(np.sqrt(np.mean(res ** 2))), bar_ok=bar_ok,
                       rotated_180=abs(rot) > 90.0, rotation_deg=rot, scale=sc, used=li.tolist(),
                       residuals_px=[float(r) for r in res], shape=tuple(a.shape), layout=layout.name,
                       n_blobs=int(len(blobs)))


# ======================================================================================================= sync / time

def _as_ty(series) -> tuple[np.ndarray, np.ndarray]:
    if hasattr(series, "t") and hasattr(series, "y"):
        t, y = series.t, series.y
    else:
        t, y = series
    t, y = np.asarray(t, dtype=np.float64), np.asarray(y, dtype=np.float64)
    if t.shape != y.shape or t.ndim != 1:
        raise ValueError(f"series needs 1-D t and y of equal length, got {t.shape} / {y.shape}")
    ok = np.isfinite(t) & np.isfinite(y)
    t, y = t[ok], y[ok]
    if t.size > 1 and np.any(np.diff(t) <= 0):
        o = np.argsort(t, kind="stable")
        t, y = t[o], y[o]
    return t, y


@dataclass(frozen=True)
class Levels:
    lo: float
    hi: float
    noise: float            # robust (MAD) sigma within the two states

    @property
    def contrast(self) -> float:
        return self.hi - self.lo


def two_levels(y) -> Levels:
    """Isodata split of a two-state signal into its low / high levels (medians) and the in-state noise."""
    y = np.asarray(y, dtype=np.float64)
    y = y[np.isfinite(y)]
    if y.size < 2 or y.max() == y.min():
        v = float(y[0]) if y.size else float("nan")
        return Levels(v, v, 0.0)
    thr = 0.5 * (y.min() + y.max())
    for _ in range(100):
        a, b = y[y <= thr], y[y > thr]
        if not a.size or not b.size:
            break
        new = 0.5 * (a.mean() + b.mean())
        if abs(new - thr) <= 1e-12 * max(1.0, abs(thr)):
            break
        thr = new
    a, b = y[y <= thr], y[y > thr]
    lo, hi = float(np.median(a)), float(np.median(b))
    mad = lambda v, c: 1.4826 * float(np.median(np.abs(v - c)))  # noqa: E731
    noise = math.sqrt((mad(a, lo) ** 2 * a.size + mad(b, hi) ** 2 * b.size) / y.size)
    return Levels(lo, hi, noise)


@dataclass(frozen=True)
class Edge:
    """A sync transition: ``t`` = pts-axis time of the half-height crossing (linear between the bracketing frames);
    ``i`` = index of the first frame past it; ``span_s`` = the bracketing pts gap (> 1 frame period = a dropped
    frame at the edge, i.e. a coarser timing). The constant offset to the true onset (exposure/readout row) is the
    camera's."""
    t: float
    kind: str
    i: int
    span_s: float


def sync_edges(series, threshold: float | None = None, *, hysteresis: float = 0.25,
               min_contrast: float = 8.0) -> list[Edge]:
    """On/off transitions of a sync-patch series (``Series``, ``(t, y)`` or ``rs["sync"]``) with sub-frame timing.

    ``threshold`` = the crossing level; ``None`` -> midway between the two levels found by :func:`two_levels`. A
    state change needs the signal to pass ``threshold +- hysteresis x contrast`` (noise cannot chatter); the reported
    time is where the series crosses the threshold itself, linear between the two bracketing frames on pts. With >= 2
    frames on the transition (exposure >= 2 frame periods, or a slow panel) that is exact up to a constant camera
    offset; with a single transition frame the linear bracket carries a phase-dependent bias of up to ~0.09 frame.
    No edges when the two levels are not separated by ``min_contrast`` x the in-state noise (never toggles).
    """
    t, y = _as_ty(series)
    if t.size < 2:
        return []
    lv = two_levels(y)
    if threshold is None:
        if not lv.contrast > 0 or lv.contrast < min_contrast * lv.noise:
            return []
        mid = 0.5 * (lv.lo + lv.hi)
        band = hysteresis * lv.contrast
    else:
        mid = float(threshold)
        a, b = y[y < mid], y[y >= mid]
        if not a.size or not b.size:
            return []
        band = hysteresis * (float(np.median(b)) - float(np.median(a)))
    up, dn = mid + band, mid - band
    state = y[0] >= mid
    lb = 0
    edges: list[Edge] = []
    for k in range(1, t.size):
        rise = (not state) and y[k] >= up
        fall = state and y[k] <= dn
        if not (rise or fall):
            continue
        j = k
        if rise:
            while j - 1 > lb and y[j - 1] >= mid:
                j -= 1
            ok = y[j - 1] < mid <= y[j]
        else:
            while j - 1 > lb and y[j - 1] <= mid:
                j -= 1
            ok = y[j - 1] > mid >= y[j]
        if ok:
            te = t[j - 1] + (mid - y[j - 1]) / (y[j] - y[j - 1]) * (t[j] - t[j - 1])
        else:
            te = t[j]
        edges.append(Edge(float(te), "rise" if rise else "fall", int(j), float(t[j] - t[j - 1])))
        state = rise
        lb = k
    return edges


@dataclass
class Folded:
    """A series folded on a period: per-sample ``phase`` in [0, 1) and ``cycle`` index, plus per-bin statistics."""
    phase: np.ndarray
    cycle: np.ndarray
    y: np.ndarray
    centers: np.ndarray
    mean: np.ndarray
    std: np.ndarray
    n: np.ndarray
    period_s: float
    t0: float


def fold_by_period(series, period_s: float, *, t0: float | None = None, bins: int = 24) -> Folded:
    """Fold on ``period_s`` from ``t0`` (default: the first sample) - beat/phase analysis on pts, so a dropped frame
    does not shift the phase of everything after it. ``bins=2`` on the panel refresh period = refresh-parity classes."""
    if not period_s > 0:
        raise ValueError(f"period_s must be > 0, got {period_s}")
    if bins < 1:
        raise ValueError("bins must be >= 1")
    t, y = _as_ty(series)
    t0 = float(t[0]) if t0 is None and t.size else float(t0 or 0.0)
    x = (t - t0) / period_s
    cyc = np.floor(x + 1e-9).astype(np.int64)
    ph = np.clip(x - cyc, 0.0, np.nextafter(1.0, 0.0))
    idx = np.minimum((ph * bins).astype(int), bins - 1)
    n = np.bincount(idx, minlength=bins)
    s1 = np.bincount(idx, weights=y, minlength=bins)
    s2 = np.bincount(idx, weights=y * y, minlength=bins)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(n > 0, s1 / np.maximum(n, 1), np.nan)
        var = np.where(n > 0, s2 / np.maximum(n, 1) - mean ** 2, np.nan)
    return Folded(phase=ph, cycle=cyc, y=y, centers=(np.arange(bins) + 0.5) / bins, mean=mean,
                  std=np.sqrt(np.clip(var, 0.0, None)), n=n, period_s=float(period_s), t0=t0)


def frame_slots(t, dt: float | None = None) -> np.ndarray:
    """Gap-aware frame numbers from pts: ``round((t - t[0]) / dt)`` (dt = median interval). A dropped frame skips a
    slot, so ``frame_slots(t) % 2`` is a true capture parity where ``arange(n) % 2`` silently flips after a drop."""
    t = np.asarray(t, dtype=np.float64)
    if t.size == 0:
        return np.zeros(0, np.int64)
    if dt is None:
        d = np.diff(t)
        dt = float(np.median(d)) if d.size else 1.0
    return np.rint((t - t[0]) / dt).astype(np.int64)


def epochs(series, times: Iterable[float], grid_s) -> tuple[np.ndarray, np.ndarray]:
    """Epoch matrix: the series interpolated (on pts) at ``time + grid_s`` for every event time whose window lies
    inside the series. Returns ``(E [n_kept, len(grid)], kept_times)``."""
    t, y = _as_ty(series)
    g = np.asarray(grid_s, dtype=np.float64)
    rows, kept = [], []
    for t0 in times:
        if t.size and t0 + g[0] >= t[0] and t0 + g[-1] <= t[-1]:
            rows.append(np.interp(t0 + g, t, y))
            kept.append(float(t0))
    return (np.asarray(rows).reshape(len(rows), g.size), np.asarray(kept))
