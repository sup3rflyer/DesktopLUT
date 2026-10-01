"""Shared scaffold for the 2026-10 hardware-session probes (charter: docs/hw-session-charter-2026-10-01.md).

Used by ``probe_near_black.py`` (BenQ HW-A/B/C), ``probe_ld_additivity.py`` (PA local-dimming pilot),
``probe_full_stack_spots.py`` (PA HDR full-stack spots) and ``probe_hw10_grayscale_roundtrip.py`` (pipe
only). It reuses the plumbing of the FALD probe sessions (``dlc.stages.fald_profile._open_session``,
``agent_fald_leak_probe.py``) instead of inventing new pieces:

* **audit first** — ``dlc.neutral_audit.neutral_state_audit`` on ``state()['layers']`` (the pipe is
  authoritative, the ini is cross-checked) BEFORE anything changes; recorded in the evidence. A native
  probe then enters the native state (``calibration.enter`` + an IDENTITY MHC2 associated — enter alone
  does not clear Windows' last MHC2 — runtime cube cleared, every viewing layer off incl. FALD) and
  REFUSES unless the audit is clean. Measure-through-the-stack probes need ``--through-stack`` (owner
  exception 2026-10-01) and still audit + record.
* **persistent meter** (``Argyll.open_persistent`` + ``make_persistent_spotread_meter``) with the
  display/mode's own colorimeter correction (``resolve_correction``; the why is recorded).
* **dogegen daemon ``shapes`` frames** (background + ONE rectangle at most — Resolve-transport safe, so
  the default daemon works; ``--stdin`` is not needed). The presenter repaints only when the frame
  changes; a DesktopLUT state change forces a redraw.
* **settle DETECTION, never a fixed settle time** — each patch is read until a tail of >= 4 good reads
  (3 consecutive steps, ``characterize.Characterizer.settle``'s ``settle_required``) spanning >=
  ``--settle-min-span-s`` (2 s; the LD probe sizes it by Dynamic Dimming speed) has every read within
  max(0.5 %, 0.003 nit) + 3·σ of its median and no linear drift beyond the same, σ = the PRIOR i1D3 read
  noise (max(0.15 %, 5e-4 nit)) — never self-estimated from the tail, so a contaminated first read or a
  one-read step cannot pass; the tail then grows to the needed reads. Bounded by ``--settle-max-s``
  (default 60 s, sized for the PA's slowest Dynamic Dimming speed, Gradual) — an unsettled patch is
  FLAGGED (anomaly), never silently accepted.
* **≥ 5 kept reads below 1 nit, ≥ 3 otherwise**; every read logged to ``reads.jsonl`` (wall time,
  time since the frame was painted, code at the meter, XYZ, spotread's ok / warning, the frame geometry
  in px). A read spotread flags (under-range / unreliable / garbled) is logged, never averaged in; a dead
  meter (self-heal exhausted, or 3 patches without a usable read) stops the run.
* **transport check**: one mid-grey 600-px sanity read at the start (wrong daemon bit depth / monitor /
  meter spot / rectangle transport ⇒ refuse; ``--skip-transport-check``).
* **park on black** at start and end; idle between patches per ``--idle-between`` (dim by default;
  the near-black probe idles on black).
* run dir under ``runs/probes/`` with ``events.jsonl`` (``check_in`` evidence packets at 25/50/75 % and
  every 180 s, ``anomaly`` events — consume them with ``runs/_watch_events.py``), ``evidence.json``,
  ``control.json`` cancel, and one ``.ti3`` per phase (and per condition).
* ``--simulate``: the file-backed DesktopLUT mock + a synthetic panel/meter on a VIRTUAL clock (no
  pipe, no meter, no daemon, no sleeps) — exercises the whole flow incl. restore; ``--phase plan``
  prints the patch list + an estimated duration without touching anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

ROOT = Path(__file__).resolve().parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dlc import neutral_audit  # noqa: E402
from dlc._pq import eotf_norm, oetf_norm  # noqa: E402
from dlc.events import EventWriter  # noqa: E402
from dlc.paths import atomic_write_text, runs_dir  # noqa: E402

# ----------------------------------------------------------------------------- constants (σ / JND class only)
SUBNIT_NITS = 1.0                 # below this a patch needs >= READS_SUBNIT kept reads (charter standing rule)
READS_SUBNIT = 5
READS_BRIGHT = 3
SETTLE_REQUIRED = 3               # consecutive in-tolerance steps (characterize.settle_required) -> >= 4-read tail
SETTLE_REL_TOL = 0.005            # 0.5 % — the overlay-path flat dip class; well above the i1d3 count quantum
SETTLE_ABS_TOL = 0.003            # cd/m² — the i1d3 low-light read floor (SyntheticFaldPanel floor)
SETTLE_K = 3.0                    # spread / drift allowance in units of the PRIOR read σ
SIGMA_REL = 0.0015                # prior i1D3 read σ: 0.15 % ...
SIGMA_ABS = 5e-4                  # ... or 5e-4 cd/m², whichever is larger (noise_floor)
SETTLE_MIN_SPAN_S = 2.0           # a settled tail spans >= 2 s (a slow ramp cannot hide inside 4 fast reads)
SETTLE_MAX_S = 60.0               # sized for the slowest Dynamic Dimming speed (Gradual); exceeding it FLAGS
SETTLE_MAX_READS = 200            # runaway backstop only — the time bound governs
KEEPOUT_PX = 120                  # fald-lessons item 4: bright content >= 120 px from the meter centre
I1D3_BODY_MM = (37.0, 65.0)       # the meter body footprint (fald.profile.PanelGeometry.body_mm)
CHECKIN_FRACTIONS = (0.25, 0.5, 0.75)
CHECKIN_EVERY_S = 180.0
PRESENT_DWELL_S = 0.25            # frame-present latency after a paint (NOT a settle — settle is detected)
OWNER_EXCEPTION = ("owner-approved 2026-10-01 (docs/hw-session-charter-2026-10-01.md standing rules): "
                   "measure-through-the-stack exception; the layers audit still runs and is recorded")
LAYER_NAMES = ("tonemap", "desktop_gamma", "white_balance", "grayscale", "fald")


class Refusal(RuntimeError):
    """A mechanical refusal (the state is not what the probe may measure)."""


class MeterDown(RuntimeError):
    """The meter is gone (spotread self-heal exhausted / closed, or no usable read for 3 patches)."""


# ----------------------------------------------------------------------------- transfer math (pure)
def max_code(bit_depth: int) -> int:
    return (1 << int(bit_depth)) - 1


def pq_code(nits: float, bit_depth: int = 10) -> int:
    """The PQ code (``bit_depth``) whose ST 2084 decode is closest to ``nits``."""
    return int(round(oetf_norm(max(float(nits), 0.0) / 10000.0) * max_code(bit_depth)))


def pq_nits(code: float, bit_depth: int = 10) -> float:
    return eotf_norm(float(code) / max_code(bit_depth)) * 10000.0


def sdr_code(nits: float, white_nits: float, gamma: float = 2.2, bit_depth: int = 8) -> int:
    """SDR code for ``nits`` on a pure-power display of white ``white_nits`` (clipped to [0, max])."""
    rel = min(max(float(nits) / float(white_nits), 0.0), 1.0)
    return int(round(rel ** (1.0 / float(gamma)) * max_code(bit_depth)))


def sdr_nits(code: float, white_nits: float, gamma: float = 2.2, bit_depth: int = 8) -> float:
    return float(white_nits) * (max(float(code), 0.0) / max_code(bit_depth)) ** float(gamma)


def code_nits(code: float, mode: str, bit_depth: int, *, white_nits: float = 120.0, gamma: float = 2.2) -> float:
    """As-if-white nominal nits of ONE channel code (HDR: PQ decode; SDR: power law at ``white_nits``)."""
    if str(mode).upper() == "HDR":
        return pq_nits(code, bit_depth)
    return sdr_nits(code, white_nits, gamma, bit_depth)


def hdr_to_sdr_code(hdr_code: int, *, anchor_hdr_code: int = 837, gamma: float = 2.2, bit_depth: int = 8,
                    hdr_bits: int = 10) -> int:
    """SDR-fallback rule for the PA additivity set (documented in probe_ld_additivity.py): keep each HDR
    code's PQ linear light RELATIVE to the set's brightest code (``anchor_hdr_code`` ↦ SDR full scale) and
    re-encode with the SDR power law. Every mix's per-channel minor/max LINEAR ratio is preserved (up to
    the SDR quantisation, which the probe records), and the set spans the same range below the SDR white
    that the HDR set spans below the panel's peak band."""
    rel = pq_nits(hdr_code, hdr_bits) / pq_nits(anchor_hdr_code, hdr_bits)
    return int(round(min(max(rel, 0.0), 1.0) ** (1.0 / gamma) * max_code(bit_depth)))


# ----------------------------------------------------------------------------- geometry (pure)
@dataclass(frozen=True)
class Geometry:
    """Screen geometry in px for one probe: the meter spot, an optional FALD zone grid and the diagonal
    (for the i1D3 body footprint). Frames go to the daemon as normalised rectangles."""

    width: int
    height: int
    meter: tuple[int, int]
    cols: int = 0
    rows: int = 0
    diagonal_in: float = 0.0

    @property
    def px_mm(self) -> Optional[float]:
        return (self.diagonal_in * 25.4 / math.hypot(self.width, self.height)) if self.diagonal_in else None

    @property
    def cell_w(self) -> float:
        return self.width / self.cols if self.cols else 0.0

    @property
    def cell_h(self) -> float:
        return self.height / self.rows if self.rows else 0.0

    def norm(self, rect_px: Sequence[float]) -> tuple[float, float, float, float]:
        """(x0, y0, w, h) px → the daemon's normalised (x, y, cx, cy), clipped to the screen."""
        x0, y0, w, h = (float(v) for v in rect_px)
        x1, y1 = min(x0 + w, self.width), min(y0 + h, self.height)
        x0, y0 = max(0.0, x0), max(0.0, y0)
        return (round(x0 / self.width, 6), round(y0 / self.height, 6),
                round(max(0.0, x1 - x0) / self.width, 6), round(max(0.0, y1 - y0) / self.height, 6))

    def px(self, rect_norm: Sequence[float]) -> list[float]:
        x, y, cx, cy = rect_norm
        return [round(x * self.width, 1), round(y * self.height, 1), round(cx * self.width, 1), round(cy * self.height, 1)]

    def centred(self, w: float, h: float) -> tuple[float, float, float, float]:
        """A w×h px rectangle centred on the meter spot."""
        return (self.meter[0] - w / 2.0, self.meter[1] - h / 2.0, float(w), float(h))

    def window_pct(self, pct: float) -> tuple[float, float, float, float]:
        """dogegen_server ``--patch-size`` convention: a square of ``pct`` % of the SHORT side, here
        centred on the meter (100 = the full screen)."""
        if pct >= 100:
            return (0.0, 0.0, float(self.width), float(self.height))
        side = min(self.width, self.height) * float(pct) / 100.0
        return self.centred(side, side)

    def gap_px(self, rect_px: Sequence[float]) -> float:
        """Distance from the meter centre to the nearest point of a px rectangle (0 when inside)."""
        x0, y0, w, h = rect_px
        dx = max(x0 - self.meter[0], 0.0, self.meter[0] - (x0 + w))
        dy = max(y0 - self.meter[1], 0.0, self.meter[1] - (y0 + h))
        return math.hypot(dx, dy)

    def zone_of(self, x: float, y: float) -> tuple[int, int]:
        if not self.cols:
            raise ValueError("no zone grid (pass --zones)")
        return int(x // self.cell_w), int(y // self.cell_h)

    def zone_block(self, col0: int, row0: int, ncols: int, nrows: int) -> tuple[float, float, float, float]:
        return (col0 * self.cell_w, row0 * self.cell_h, ncols * self.cell_w, nrows * self.cell_h)

    def zone_centre(self, col: int, row: int) -> tuple[float, float]:
        return ((col + 0.5) * self.cell_w, (row + 0.5) * self.cell_h)

    def body_px(self) -> tuple[float, float]:
        mm = self.px_mm or 0.1845                     # PA32UCXR px pitch when the diagonal is unknown
        return (I1D3_BODY_MM[0] / mm, I1D3_BODY_MM[1] / mm)

    def as_dict(self) -> dict[str, Any]:
        return {"width": self.width, "height": self.height, "meter": list(self.meter), "cols": self.cols,
                "rows": self.rows, "diagonal_in": self.diagonal_in,
                "cell_px": [self.cell_w, self.cell_h] if self.cols else None,
                "meter_zone": list(self.zone_of(*self.meter)) if self.cols else None}


def parse_xy(text: Optional[str]) -> Optional[tuple[int, int]]:
    if not text:
        return None
    a, b = (int(round(float(v))) for v in str(text).replace("x", ",").split(","))
    return a, b


# ----------------------------------------------------------------------------- patches (pure)
Shape = tuple[tuple[int, int, int], tuple[float, float, float, float]]


@dataclass
class Patch:
    """One frame read at the meter. ``field`` = the code under the meter (the .ti3 RGB unless ``ti3_rgb``
    overrides it); ``cond`` splits the phase's .ti3 by condition (surround, cube state, …)."""

    name: str
    phase: str
    shapes: list
    field: tuple[int, int, int]
    group: str = ""
    cond: str = ""
    meta: dict = field(default_factory=dict)
    ti3_rgb: Optional[tuple[int, int, int]] = None
    min_reads: int = 0

    def __post_init__(self) -> None:
        if not self.shapes or len(self.shapes) > 2:
            raise ValueError(f"{self.name}: a frame is a background + at most ONE rectangle (Resolve-safe), got "
                             f"{len(self.shapes)} shapes")
        self.shapes = [(tuple(int(c) for c in code), tuple(float(v) for v in rect)) for code, rect in self.shapes]
        self.field = tuple(int(c) for c in self.field)

    def geometry_px(self, g: Geometry) -> list[dict[str, Any]]:
        return [{"code": list(code), "rect_px": g.px(rect)} for code, rect in self.shapes]

    def as_dict(self, g: Optional[Geometry] = None) -> dict[str, Any]:
        out = {"name": self.name, "phase": self.phase, "group": self.group, "cond": self.cond,
               "field": list(self.field), "shapes": [[list(c), list(r)] for c, r in self.shapes], "meta": self.meta}
        if self.ti3_rgb is not None:
            out["ti3_rgb"] = list(self.ti3_rgb)
        if g is not None:
            out["shapes_px"] = self.geometry_px(g)
        return out


def full(code: Sequence[int]) -> list:
    return [(tuple(code), (0.0, 0.0, 1.0, 1.0))]


def framed(g: Geometry, bg: Sequence[int], code: Sequence[int], rect_px: Sequence[float]) -> list:
    return [(tuple(bg), (0.0, 0.0, 1.0, 1.0)), (tuple(code), g.norm(rect_px))]


def grey(c: int) -> tuple[int, int, int]:
    return (int(c), int(c), int(c))


def reads_needed(y_nits: Optional[float], min_reads: int = 0) -> int:
    base = READS_SUBNIT if (y_nits is None or y_nits < SUBNIT_NITS) else READS_BRIGHT
    return max(base, int(min_reads or 0))


# ----------------------------------------------------------------------------- settle detection (pure)
def noise_floor(y: float) -> float:
    """PRIOR i1D3 read σ for one XYZ component (cd/m²): the count quantum / low-light floor class
    (D4a: one count ≈ 2.7e-4 rel above ~15 nit; sub-nit reads scatter ~1e-3 nit). A prior, never
    self-estimated from the 4-read tail — a 2-dof SE scales with an outlier and would accept it."""
    return max(SIGMA_REL * abs(float(y)), SIGMA_ABS)


def _median(v: Sequence[float]) -> float:
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def tail_settled(times: Sequence[float], xyzs: Sequence[Sequence[float]], *, rel_tol: float = SETTLE_REL_TOL,
                 abs_tol: float = SETTLE_ABS_TOL, k: float = SETTLE_K, min_span_s: float = 0.0
                 ) -> tuple[bool, dict[str, Any]]:
    """Is the read tail settled? For each of X, Y, Z, with tol = max(rel_tol·|median|, abs_tol) and the
    PRIOR read σ (:func:`noise_floor`):
      * spread — every read within tol + k·σ of the tail median (a contaminated first read, a glitch, a
        step never passes, whatever the tail's own scatter);
      * drift  — the least-squares linear drift across the tail's span within tol + k·σ (a monotone LED /
        backlight ramp fails even when each step is small);
      * span   — the tail covers at least ``min_span_s`` (a slow ramp cannot hide inside 4 fast reads).
    Needs >= 3 reads."""
    n = len(xyzs)
    if n < 3 or len(times) != n:
        return False, {"reason": "need >= 3 reads"}
    t = [float(v) for v in times]
    span = t[-1] - t[0]
    if span + 1e-9 < float(min_span_s):
        return False, {"reason": f"tail span {span:.2f} s < {min_span_s:g} s"}
    tm = sum(t) / n
    sxx = sum((v - tm) ** 2 for v in t)
    if sxx <= 0 or span <= 0:
        sxx, span, t, tm = float(n * (n * n - 1) / 12.0), float(n - 1), [float(i) for i in range(n)], (n - 1) / 2.0
    out: dict[str, Any] = {}
    ok_all = True
    for c, name in enumerate("XYZ"):
        ys = [float(v[c]) for v in xyzs]
        med = _median(ys)
        allow = max(rel_tol * abs(med), abs_tol) + k * noise_floor(med)
        spread = max(abs(y - med) for y in ys)
        ym = sum(ys) / n
        b = sum((ti - tm) * (yi - ym) for ti, yi in zip(t, ys)) / sxx
        drift = abs(b) * span
        ok = spread <= allow and drift <= allow
        ok_all &= ok
        if name == "Y" or not ok:
            out[name] = {"spread": round(spread, 6), "drift": round(drift, 6), "allow": round(allow, 6), "ok": ok}
    return ok_all, out


def settled_tail(times: Sequence[float], xyzs: Sequence[Sequence[float]], *, min_reads: int = 4,
                 min_span_s: float = 0.0, **kw) -> Optional[int]:
    """The start index of the SHORTEST suffix with >= ``min_reads`` reads spanning >= ``min_span_s`` that
    :func:`tail_settled` accepts, or None. (A fixed 4-read tail of 0.4-s bright reads could never span a
    2-s minimum; the suffix grows instead, and an early contaminated read drops out of it.)"""
    n = len(xyzs)
    for start in range(n - min_reads, -1, -1):
        if times[-1] - times[start] + 1e-9 < min_span_s:
            continue
        ok, _ = tail_settled(times[start:], xyzs[start:], min_span_s=min_span_s, **kw)
        return start if ok else None
    return None


# ----------------------------------------------------------------------------- .ti3 writer (pure)
def write_ti3(path: Path, rows: Sequence[tuple[Sequence[int], Sequence[float]]], *, bit_depth: int, title: str,
              notes: Sequence[str] = ()) -> Path:
    """Write a CTI3 in the exact layout :func:`dlc.measure_loop.write_ti3` emits (RGB as 0–100 %, XYZ
    absolute cd/m²) — what :func:`dlc.mhc.parse_ti3`, ``teardrop_fit.py`` and ``pa_additivity.py`` read."""
    mx = float(max_code(bit_depth))
    body = [" ".join([f"{c / mx * 100.0:.6f}" for c in rgb] + [f"{v:.6f}" for v in xyz]) for rgb, xyz in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(["CTI3", f"# {title}", *[f"# {n}" for n in notes], "BEGIN_DATA_FORMAT",
                      "RGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z", "END_DATA_FORMAT", f"NUMBER_OF_SETS {len(body)}",
                      "BEGIN_DATA", *body, "END_DATA", ""])
    atomic_write_text(path, text)
    return path


def read_ti3_rows(path: Path) -> list[tuple[list[float], list[float]]]:
    """(rgb 0–1, xyz) rows of a CTI3 (the same tolerant parse the replay scripts use)."""
    rows, inside = [], False
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s == "BEGIN_DATA":
            inside = True
            continue
        if s == "END_DATA":
            break
        if inside and s:
            v = [float(x) for x in s.split()]
            rows.append(([c / 100.0 for c in v[:3]], v[3:6]))
    return rows


def lut3d_size(path: Path) -> Optional[int]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(64):
                line = f.readline()
                if not line:
                    break
                m = re.match(r"\s*LUT_3D_SIZE\s+(\d+)", line)
                if m:
                    return int(m.group(1))
    except OSError:
        return None
    return None


def same_path(a: Optional[str], b: Optional[str]) -> bool:
    """Path equality as Windows sees it (case, slashes, ``..``) — a pipe readback may normalise the path."""
    import os
    if not a or not b:
        return not a and not b
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


# ----------------------------------------------------------------------------- stack snapshot / diff (pure)
def stack_snapshot(state: dict[str, Any], monitor: int) -> dict[str, Any]:
    """The restorable stack of ``monitor`` (both modes) from a ``state.get`` reply: MHC identity
    (applied / source_file / profile_name), the runtime cube path and the viewing layers."""
    out: dict[str, Any] = {}
    for mode in ("SDR", "HDR"):
        key = f"{int(monitor)}:{mode}"
        mhc = (state.get("mhc") or {}).get(key) or {}
        rt = (state.get("runtime") or {}).get(key) or {}
        lay = (state.get("layers") or {}).get(key)
        out[key] = {
            "mhc_applied": bool(mhc.get("applied") or mhc.get("enabled")),
            "mhc_source_file": mhc.get("source_file") or None,
            "mhc_profile_name": mhc.get("profile_name") or None,
            # the WB/GS/DG permutation the live ICM was baked with: a real change (an HW-10 write that never
            # re-baked leaves the ICM on the old permutation while state.get's curve looks right)
            "active_perm": mhc.get("active_perm"),
            "cube_path": rt.get("cube_path") or None,
            "layers": ({n: bool(lay.get(n)) for n in LAYER_NAMES} if isinstance(lay, dict) else None),
            "fald_params_path": (lay.get("fald_params_path") or None) if isinstance(lay, dict) else None,
        }
    return out


def stack_diff(pre: dict[str, Any], post: dict[str, Any]) -> dict[str, list]:
    """``{"changed": [...], "churn": [...]}``: what differs between two :func:`stack_snapshot`s. A
    ``profile_name`` change alone is ``churn`` (a WB/GS/DG permutation re-bake renames the ICM; the
    ``source_file`` is the identity that survives it) — everything else is a real change."""
    changed, churn = [], []
    for key in sorted(set(pre) | set(post)):
        a, b = pre.get(key) or {}, post.get(key) or {}
        for k in ("mhc_applied", "mhc_source_file", "active_perm", "cube_path", "fald_params_path"):
            same = same_path(a.get(k), b.get(k)) if k in ("mhc_source_file", "cube_path", "fald_params_path")                 else a.get(k) == b.get(k)
            if not same:
                changed.append({"pair": key, "what": k, "before": a.get(k), "after": b.get(k)})
        la, lb = a.get("layers"), b.get("layers")
        if la is not None and lb is not None:
            for n in LAYER_NAMES:
                if bool(la.get(n)) != bool(lb.get(n)):
                    changed.append({"pair": key, "what": f"layer:{n}", "before": la.get(n), "after": lb.get(n)})
        if a.get("mhc_profile_name") != b.get("mhc_profile_name"):
            churn.append({"pair": key, "what": "mhc_profile_name", "before": a.get("mhc_profile_name"),
                          "after": b.get("mhc_profile_name")})
    return {"changed": changed, "churn": churn}


# ----------------------------------------------------------------------------- read-time model (plan estimates)
def est_read_s(nits: float) -> float:
    """Persistent i1D3 read time vs luminance (persistent-meter-validated: bright 0.32 s, ~18 nit 0.67 s,
    dark < 0.4 nit ~7 s — the integration floor)."""
    y = max(float(nits), 0.0)
    if y >= 20.0:
        return 0.4
    if y >= 5.0:
        return 0.7
    if y >= 1.0:
        return 1.3
    if y >= 0.4:
        return 3.0
    if y >= 0.1:
        return 5.5
    return 7.5


def est_patch_s(nits: float, settle_extra_reads: int = 0, min_reads: int = 0,
                min_span_s: float = SETTLE_MIN_SPAN_S) -> float:
    r = est_read_s(nits)
    n = max(SETTLE_REQUIRED + 1, reads_needed(nits, min_reads), int(math.ceil(min_span_s / r)) + 1) + settle_extra_reads
    return PRESENT_DWELL_S + n * r + 0.2


def fmt_min(seconds: float) -> str:
    return f"{seconds / 60.0:.1f} min"


# ----------------------------------------------------------------------------- synthetic panel (--simulate / plan)
def _xy_to_xyz(x: float, y: float, Y: float = 1.0) -> tuple[float, float, float]:
    return (x * Y / y, Y, (1.0 - x - y) * Y / y)


def _solve3(m: list[list[float]], v: list[float]) -> list[float]:
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for i in range(3):
        p = max(range(i, 3), key=lambda r: abs(a[r][i]))
        a[i], a[p] = a[p], a[i]
        for r in range(3):
            if r != i:
                f = a[r][i] / a[i][i]
                a[r] = [a[r][k] - f * a[i][k] for k in range(4)]
    return [a[i][3] / a[i][i] for i in range(3)]


def rgb_to_xyz_matrix(prims: dict[str, tuple[float, float]], white_xy: tuple[float, float]) -> list[list[float]]:
    """Columns = each primary's XYZ such that RGB (1,1,1) → the white with Y = 1."""
    cols = [_xy_to_xyz(*prims[c]) for c in "RGB"]
    m = [[cols[j][i] for j in range(3)] for i in range(3)]
    s = _solve3(m, list(_xy_to_xyz(*white_xy)))
    return [[m[i][j] * s[j] for j in range(3)] for i in range(3)]


SIM_PRIMS = {
    "lcd_srgb": {"R": (0.640, 0.330), "G": (0.300, 0.600), "B": (0.150, 0.060)},
    "fald_wide": {"R": (0.692, 0.307), "G": (0.232, 0.700), "B": (0.152, 0.051)},
}


@dataclass
class SimPanel:
    """A plausible synthetic panel for ``--simulate`` (plumbing, not physics truth):

    * ``lcd`` (BenQ-like, no local dimming): XYZ = own(code) + a constant pedestal that is the 0,0,0 read
      (0.054 nit, xy .276/.285) on an ALL-BLACK frame and 1.7× brighter / bluer (0.092, .260/.260) as
      soon as anything in the frame is lit — the HW-A hypothesis H1; a production cube crushes non-grey
      codes <= 8 (shell 1) to black (HW-C).
    * ``fald`` (PA-like mini-LED): LD on — pedestal = 0.0506·L^0.503 along K (the 132412 level-edge law,
      L = the max channel's nits at the meter), the minor channels lose a little light at a small
      minor/max ratio (a backlight-mediated non-additivity), a halo glow on black beside a window, and a
      Dynamic-Dimming transient after every frame change (τ by speed); LD off — a constant 1.9-nit
      (HDR) / 0.15-nit (SDR) pedestal, additive.
    Noise: 0.2 % + 0.0008 nit, seeded."""

    kind: str
    mode: str
    bit_depth: int
    geometry: Geometry
    white_nits: float = 120.0
    gamma: float = 2.2
    peak_nits: float = 1729.0
    ld_on: bool = True
    dimming_tau_s: float = 0.15
    seed: int = 7
    stack: Callable[[], dict[str, Any]] = field(default=lambda: {})
    rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        prims = SIM_PRIMS["lcd_srgb" if self.kind == "lcd" else "fald_wide"]
        self.M = rgb_to_xyz_matrix(prims, (0.3127, 0.3290))

    def chan_nits(self, code: Sequence[int]) -> list[float]:
        out = []
        for c in code:
            n = code_nits(c, self.mode, self.bit_depth, white_nits=self.white_nits, gamma=self.gamma)
            out.append(min(n, self.peak_nits) if self.mode.upper() == "HDR" else n)
        return out

    def _own(self, code: Sequence[int]) -> list[float]:
        L = self.chan_nits(code)
        if self.kind == "fald" and self.ld_on and max(L) > 0:
            mx = max(L)
            L = [li * (1.0 if li >= mx else 0.96 + 0.04 * (li / mx)) for li in L]
        return [sum(self.M[i][j] * L[j] for j in range(3)) for i in range(3)]

    def expected(self, shapes: Sequence[Shape], meter_norm: tuple[float, float], since_paint_s: float = 1e9
                 ) -> tuple[float, float, float]:
        mx, my = meter_norm
        at = shapes[0][0]
        for code, (x, y, cx, cy) in shapes:
            if x <= mx <= x + cx and y <= my <= y + cy:
                at = code
        code = list(at)
        st = self.stack() or {}
        if st.get("cube") == "production" and self.kind == "lcd":
            if max(code) <= 8 * (1 << (self.bit_depth - 8)) and len(set(code)) > 1:
                code = [0, 0, 0]
        own = self._own(code)
        lit = any(max(c) > 0 for c, _r in shapes)
        if self.kind == "lcd":
            ped = _xy_to_xyz(0.260, 0.260, 0.092) if lit else _xy_to_xyz(0.276, 0.285, 0.054)
        elif self.ld_on:
            L = max(self.chan_nits(code))
            ped = _xy_to_xyz(0.259, 0.303, 0.0506 * L ** 0.503) if L > 0 else (0.0, 0.0, 0.0)
            if max(code) == 0 and lit:                                       # halo on black beside a window
                g = self.geometry
                best = 0.0
                for c, r in shapes[1:]:
                    gap = g.gap_px(g.px(r))
                    best = max(best, 0.004 * max(self.chan_nits(c)) * math.exp(-gap / 150.0))
                ped = _xy_to_xyz(0.259, 0.303, best) if best > 0 else ped
        else:
            ped = _xy_to_xyz(0.259, 0.303, 1.9 if self.mode.upper() == "HDR" else 0.15)
        xyz = [own[i] + ped[i] for i in range(3)]
        if self.kind == "fald" and self.ld_on and since_paint_s < 50 * self.dimming_tau_s:
            f = 1.0 - 0.08 * math.exp(-since_paint_s / max(self.dimming_tau_s, 1e-3))
            xyz = [v * f for v in xyz]
        return (xyz[0], xyz[1], xyz[2])

    def read(self, shapes, meter_norm, since_paint_s) -> tuple[float, float, float]:
        xyz = self.expected(shapes, meter_norm, since_paint_s)
        return tuple(max(0.0, v * (1.0 + self.rng.gauss(0.0, 0.002)) + self.rng.gauss(0.0, 0.0008)) for v in xyz)


# ----------------------------------------------------------------------------- clocks / presenters
class RealClock:
    virtual = False

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, s: float) -> None:
        if s > 0:
            time.sleep(s)


class VirtualClock:
    virtual = True

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, float(s))


class _PresenterMixin:
    """Repaint only when the frame changes (repeat reads of one patch never re-flash or re-dwell);
    ``invalidate`` forces the next show to repaint (after a DesktopLUT state change — a setting change
    is not a new frame, fald-lessons item 6)."""

    clock: Any
    dwell_s: float
    last: Optional[list] = None
    last_paint_t: Optional[float] = None
    pending: Optional[list] = None

    def _paint_raw(self, shapes) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def paint(self, shapes) -> None:
        self._paint_raw(shapes)
        self.last = [tuple(s) for s in shapes]
        self.last_paint_t = self.clock.now()

    def invalidate(self) -> None:
        self.last = None

    def show(self, patch) -> None:
        if self.pending is None:
            raise RuntimeError("show without a pending frame")
        if self.last != [tuple(s) for s in self.pending]:
            self.paint(self.pending)
            self.clock.sleep(self.dwell_s + float(getattr(patch, "settle_bump_s", 0.0) or 0.0))


def make_hw_presenter(host: str, port: int, clock, dwell_s: float):
    from dlc.fald.shapes import ShapesPresenter

    class StickyShapesPresenter(_PresenterMixin, ShapesPresenter):
        def __init__(self) -> None:
            ShapesPresenter.__init__(self, host, port, settle_seconds=dwell_s)
            self.clock, self.dwell_s = clock, dwell_s

        def _paint_raw(self, shapes) -> None:
            ShapesPresenter.paint(self, shapes)

    return StickyShapesPresenter()


class SimPresenter(_PresenterMixin):
    def __init__(self, clock, dwell_s: float) -> None:
        self.clock, self.dwell_s = clock, dwell_s
        self.frames = 0

    def _paint_raw(self, shapes) -> None:
        for _code, rect in shapes:
            if any(v < 0 or v > 1 for v in rect):
                raise ValueError(f"shapes: geometry out of [0,1]: {rect}")
        self.frames += 1

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


def make_reader(presenter, measure: Callable, bit_depth: int) -> Callable:
    """``read(label, shapes, field_code) -> (xyz | None, ok, error, meter_fault)`` — like
    :func:`dlc.fald.shapes.make_shapes_reader`, but it keeps spotread's verdict: a reading that came
    with an under-range / unreliable warning or a garbled line has XYZ and ``ok=False``
    (``argyll.PersistentSpotread._classify_locked``); the probe logs it and never averages it in."""
    from dlc.measure_loop import MeasurePatch
    mx = float(max_code(bit_depth))

    def read(label: str, shapes: list, field_code: tuple):
        presenter.pending = shapes
        patch = MeasurePatch(label=label, rgb=tuple(int(c) for c in field_code),
                             signal=tuple(c / mx for c in field_code), role="measurement", bit_depth=bit_depth, seq=0)
        rd = measure(patch)
        xyz = tuple(float(v) for v in rd.xyz) if rd.xyz is not None else None
        fault = (rd.raw or {}).get("meter_fault") if isinstance(getattr(rd, "raw", None), dict) else None
        return xyz, bool(rd.ok and xyz is not None), (None if rd.ok else str(rd.error)), fault

    return read


# ----------------------------------------------------------------------------- CLI
def add_common_args(p: argparse.ArgumentParser, *, mode: str, bit_depth: int, monitor: int) -> None:
    """The flags every meter probe shares (on top of ``dlc.stages._common.base_parser``)."""
    p.set_defaults(mode=mode, monitor=monitor)
    p.add_argument("--bit-depth", type=int, default=bit_depth, dest="bit_depth")
    p.add_argument("--dogegen-server", default="127.0.0.1:28930", dest="dogegen_server")
    p.add_argument("--profile", default=None, help="calibration_profile.yaml (default: the DLC root one)")
    p.add_argument("--meter", default=None, help="meter spot X,Y in px (default: the probe's documented spot)")
    p.add_argument("--screen", default=None, help="WxH px override for --phase plan (no pipe)")
    p.add_argument("--idle-between", choices=("dim", "black", "none"), default="dim", dest="idle_between",
                   help="frame shown between patches (default dim; start/end always park on black)")
    p.add_argument("--settle-max-s", type=float, default=SETTLE_MAX_S, dest="settle_max_s")
    p.add_argument("--settle-max-reads", type=int, default=SETTLE_MAX_READS, dest="settle_max_reads")
    p.add_argument("--settle-rel-tol", type=float, default=SETTLE_REL_TOL, dest="settle_rel_tol")
    p.add_argument("--settle-abs-tol", type=float, default=SETTLE_ABS_TOL, dest="settle_abs_tol")
    p.add_argument("--settle-min-span-s", type=float, default=None, dest="settle_min_span_s",
                   help=f"a settled tail spans at least this (default {SETTLE_MIN_SPAN_S:g} s; the LD probe sizes it by "
                        "--dimming-speed)")
    p.add_argument("--skip-transport-check", action="store_true", dest="skip_transport_check",
                   help="skip the start-of-session mid-grey sanity read (bit depth / monitor / rectangle transport)")
    p.add_argument("--present-dwell-s", type=float, default=PRESENT_DWELL_S, dest="present_dwell_s",
                   help="frame-present latency after a repaint (not a settle — settle is detected)")
    p.add_argument("--tag", default="", help="suffix for the run directory name")


# ----------------------------------------------------------------------------- the session
@dataclass
class PatchResult:
    patch: Patch
    xyz: Optional[tuple[float, float, float]]
    n_kept: int
    settled: bool
    settle_s: Optional[float]
    reads: list = field(default_factory=list)
    kept: list = field(default_factory=list)
    sd_y: Optional[float] = None
    state: str = ""
    error: Optional[str] = None

    @property
    def y(self) -> Optional[float]:
        return self.xyz[1] if self.xyz else None

    def as_dict(self, g: Optional[Geometry] = None) -> dict[str, Any]:
        xy = None
        if self.xyz and sum(self.xyz) > 0:
            s = sum(self.xyz)
            xy = [round(self.xyz[0] / s, 5), round(self.xyz[1] / s, 5)]
        return {"patch": self.patch.as_dict(g), "state": self.state, "xyz": list(self.xyz) if self.xyz else None,
                "xy": xy, "n_kept": self.n_kept, "sd_y": self.sd_y, "settled": self.settled,
                "settle_s": self.settle_s, "kept_read_indices": self.kept, "n_reads": len(self.reads),
                "error": self.error}


class ProbeSession:
    """One probe invocation: run dir, events, controller, audits, (optional) native state, meter."""

    def __init__(self, args, probe: str, *, run_dir: Optional[Path] = None, need_meter: bool = True) -> None:
        self.args = args
        self.probe = probe
        self.monitor = int(args.monitor)
        self.mode = str(args.mode).upper()
        self.key = f"{self.monitor}:{self.mode}"
        self.simulate = bool(getattr(args, "simulate", False))
        self.clock = VirtualClock() if self.simulate else RealClock()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if run_dir is None:
            tag = ("_" + args.tag) if getattr(args, "tag", "") else ""
            if getattr(args, "run", None):
                run_dir = Path(args.run)                     # the operator's choice, used as is
            else:
                base = run_dir = runs_dir() / "probes" / (
                    f"{stamp}_{probe}_{self.mode.lower()}_mon{self.monitor}{'_sim' if self.simulate else ''}{tag}")
                k = 2
                while run_dir.exists():                      # two invocations in one second must not share a dir
                    run_dir = base.with_name(f"{base.name}_{k}")
                    k += 1
        self.root = Path(run_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.events = EventWriter(self.root / "events.jsonl")
        self.reads_path = self.root / "reads.jsonl"
        self.evidence: dict[str, Any] = {"probe": probe, "started": datetime.now().isoformat(timespec="seconds"),
                                         "argv": sys.argv[1:], "simulate": self.simulate, "monitor": self.monitor,
                                         "mode": self.mode, "run_dir": str(self.root), "audits": {}, "warnings": []}
        self.controller = None
        self.entered = False
        self.pre_state: Optional[dict[str, Any]] = None
        self.presenter = None
        self.meter = None
        self.read_fn = None
        self.panel: Optional[SimPanel] = None
        self.geometry: Optional[Geometry] = None
        self.need_meter = need_meter
        self.t0 = self.clock.now()
        self.n_reads = 0
        self.no_read_streak = 0
        self.anomalies: list[dict[str, Any]] = []
        self._ci_fired: set = set()
        self._ci_last = self.t0
        self._ci_seq = 0
        self.idle_code: Optional[tuple[int, int, int]] = None
        self._mid_code = 0
        self.sim_stack: dict[str, Any] = {}

    # -- evidence / events ------------------------------------------------------------------
    def log(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    def event(self, event: str, level: str = "INFO", tier: str = "digest", **data: Any) -> None:
        self.events.write(level, self.probe, event, tier=tier, **data)

    def anomaly(self, kind: str, **data: Any) -> None:
        row = {"kind": kind, **data}
        self.anomalies.append(row)
        self.event("anomaly", level="WARN", evidence=row, **row)        # evidence: what _watch_events.py prints
        self.log(f"   !! anomaly {kind}: {json.dumps(data, default=str)[:300]}")

    def save_evidence(self) -> Path:
        self.evidence["anomalies"] = self.anomalies
        self.evidence["elapsed_s"] = round(self.clock.now() - self.t0, 1)
        self.evidence["reads_total"] = self.n_reads
        p = self.root / "evidence.json"
        atomic_write_text(p, json.dumps(self.evidence, indent=1, default=str))
        return p

    def cancel_requested(self) -> bool:
        p = self.root / "control.json"
        try:
            return p.exists() and json.loads(p.read_text(encoding="utf-8")).get("action") == "cancel"
        except (OSError, ValueError):
            return False

    # -- controller ---------------------------------------------------------------------------
    def connect(self):
        from dlc.controller import CalibrationController
        from dlc.stages._common import SIM_STATE_FILE, FileBackedMockTransport
        if self.simulate:
            self.controller = CalibrationController.with_transport(FileBackedMockTransport(self.root / SIM_STATE_FILE))
        else:
            self.controller = CalibrationController.connect(getattr(self.args, "pipe", None) or
                                                            r"\\.\pipe\DesktopLUT.Calibration")
        return self.controller

    def profile(self):
        from dlc import calibration_profile as cp
        if not hasattr(self, "_profile"):
            self._profile = cp.load_profile(getattr(self.args, "profile", None))
        return self._profile

    def ini_path(self) -> Optional[Path]:
        if self.simulate:
            return None
        try:
            return neutral_audit.resolve_desktoplut_ini(self.profile().paths, cwd=ROOT)
        except Exception:  # noqa: BLE001 - the audit notes a missing ini; the pipe stays authoritative
            return None

    def monitor_info(self) -> dict[str, Any]:
        mons = (self.controller.query_monitors() or {}).get("monitors") or []
        mon = next((m for m in mons if m.get("index") == self.monitor), None)
        if mon is None:
            raise Refusal(f"monitor {self.monitor} not in query_monitors ({[m.get('index') for m in mons]})")
        return mon

    def preflight(self) -> dict[str, Any]:
        """Pipe alive, monitor present, colour space matches the mode, bit depth vs the link; the
        evidence records ``contract_version`` (HW-11) and the link format."""
        st = self.controller.state() or {}
        mon = self.monitor_info()
        cs = str(mon.get("color_space") or "")
        info = {"contract_version": st.get("contract_version"), "color_space": cs, "hdr_active": mon.get("hdr_active"),
                "rect": mon.get("rect"), "link_bpc": mon.get("link_bpc"), "link_color_encoding": mon.get("link_color_encoding"),
                "friendly_name": mon.get("friendly_name"), "hook": st.get("hook"), "overlay": st.get("overlay")}
        self.evidence["preflight"] = info
        if self.mode == "HDR" and cs and cs != "HDR":
            raise Refusal(f"mode HDR but monitor {self.monitor} reports {cs} — switch HDR on first")
        if self.mode == "SDR" and cs == "HDR":
            raise Refusal(f"mode SDR but monitor {self.monitor} is in HDR")
        bd = getattr(self.args, "bit_depth", None)
        if bd and mon.get("link_bpc") and int(bd) > int(mon["link_bpc"]):
            self.evidence["warnings"].append(f"--bit-depth {bd} exceeds the link's {mon['link_bpc']} bpc")
        return info

    def screen(self) -> tuple[int, int]:
        rect = (self.evidence.get("preflight") or {}).get("rect") or {}
        if rect.get("width") and rect.get("height"):
            return int(rect["width"]), int(rect["height"])
        override = parse_xy(getattr(self.args, "screen", None))
        return override or (3840, 2160)

    def snapshot(self) -> dict[str, Any]:
        return stack_snapshot(self.controller.state() or {}, self.monitor)

    # -- audit ----------------------------------------------------------------------------------
    def audit(self, tag: str) -> dict[str, Any]:
        a = neutral_audit.neutral_state_audit(self.controller, self.monitor, self.mode, ini_path=self.ini_path())
        a["layers_on"] = list(a.get("gui_layers_enabled") or [])
        a["at"] = datetime.now().isoformat(timespec="seconds")
        self.evidence["audits"][tag] = a
        self.event("audit", tag=tag, layers_on=a["layers_on"], source=a.get("gui_layers_source"),
                   profile_name=a.get("profile_name"), cube=(a.get("runtime") or {}).get("cube_path"),
                   notes=a.get("notes"))
        self.log(f"[audit:{tag}] {self.key} layers ON {a['layers_on'] or 'none'} (source {a.get('gui_layers_source')}); "
                 f"mhc profile {a.get('profile_name')}; cube {(a.get('runtime') or {}).get('cube_path')}; "
                 f"hook {a.get('hook')}; overlay {a.get('overlay')}")
        return a

    def require_through_stack(self, what: str) -> None:
        """The owner exception: refuse without ``--through-stack``; with it, record the approval."""
        if not getattr(self.args, "through_stack", False):
            raise Refusal(f"{what} measures THROUGH the applied stack — the hard rule refuses that; the owner "
                          "approved it as an exception for this probe (2026-10-01): re-run with --through-stack")
        self.evidence["through_stack"] = {"flag": "--through-stack", "approval": OWNER_EXCEPTION, "what": what}
        self.event("through_stack_exception", what=what, approval=OWNER_EXCEPTION)

    # -- native state (DLC raw-stage equivalent) -----------------------------------------------------
    def backup_ini(self) -> None:
        ini = self.ini_path()
        if ini and Path(ini).exists():
            dst = self.root / "DesktopLUT.ini.pre-probe"
            shutil.copy2(ini, dst)
            self.evidence["ini_backup"] = str(dst)

    def _identity_native_primaries(self):
        if self.mode != "HDR" or self.simulate:
            return None
        try:
            from dlc.calibrate import dip_record_for, dip_store_path
            from dlc.dip import DipStore
            prof = self.profile()
            rec = dip_record_for(DipStore.load(dip_store_path(prof, self.root)), prof.display_for(self.monitor).name, self.mode)
            return getattr(rec, "native_primaries", None) if rec else None
        except Exception:  # noqa: BLE001 - bootstrap primaries give identity in HDR as well
            return None

    def enter_native(self) -> dict[str, Any]:
        """calibration.enter + identity MHC2 (DIP native primaries in HDR, Rec.709 in SDR) + cube cleared +
        every viewing layer off; then audit and REFUSE unless clean. The user's stack is captured by
        DesktopLUT's snapshot at the enter (layers are switched off only AFTER it, so the restore puts them back)."""
        from dlc.profiles import default_dummy_icc, resolve_profile_path
        from dlc.stages import _common
        ctl = self.controller
        stale = _common.stale_calibration_session(ctl)
        if stale:
            raise Refusal(f"an earlier calibration session is still open ({stale}) — exit/restore it first "
                          "(a probe must not stack on another session's capture)")
        self.backup_ini()
        dummy = default_dummy_icc(self.mode)
        # None = unknown: a client-side failure (pipe timeout) cannot prove the server did NOT enter — the
        # restore is then still requested (request_snapshot_restore gates on what DesktopLUT holds)
        self.entered = None
        enter = ctl.enter_neutral(self.monitor, self.mode, str(resolve_profile_path(dummy.path)), reason=f"DLC {self.probe}")
        self.entered = True
        rec: dict[str, Any] = {"calibration_enter": enter,
                               "snapshot_retained": enter.get("snapshot_retained") if isinstance(enter, dict) else None}
        prim, src = neutral_audit.identity_primaries(self.mode, self._identity_native_primaries())
        ctl.set_primaries(self.monitor, self.mode, prim)
        ctl.set_white(self.monitor, self.mode, *neutral_audit.D65_XY)
        ctl.apply_mhc(self.monitor, self.mode)
        rec["identity_mhc"] = {"primaries": prim, "source": src, "white": list(neutral_audit.D65_XY)}
        self.clock.sleep(3.0)
        st = self.controller.state() or {}
        cube = ((st.get("runtime") or {}).get(self.key) or {}).get("cube_path")
        if cube:
            ctl.clear_3dlut(self.monitor, self.mode)
            rec["cube_cleared"] = cube
        from dlc.controller import CalibrationController
        lay = CalibrationController.layers_from_state(self.controller.state() or {}, self.monitor, self.mode)
        on = {n: False for n, v in (lay or {}).items() if v}
        if on:
            res = ctl.set_layers(self.monitor, self.mode, **on)
            rec["layers_switched_off"] = sorted(on)
            rec["layers_set_reply"] = res
        self.evidence["native"] = rec
        self.log(f"[native] entered calibration mode; identity MHC ({src}); cube {'cleared' if cube else 'none'}; "
                 f"layers off: {sorted(on) or 'none were on'}")
        a = self.audit("after_native")
        bad = neutral_audit.neutral_violations(a, require_profile=True)
        if (a.get("runtime") or {}).get("cube_path"):
            bad.append(f"a runtime 3D LUT is still loaded for {self.key}: {(a.get('runtime') or {}).get('cube_path')}")
        if bad:
            raise Refusal("REFUSING to read: the native state is not clean — " + "; ".join(bad))
        return rec

    def restore(self) -> dict[str, Any]:
        """exit(restore_snapshot=True), then verify the stack against the pre-probe snapshot and put back
        anything the restore missed that a probe can put back (runtime cube, viewing layers)."""
        from dlc.stages import _common
        out: dict[str, Any] = {}
        if self.entered is not False:
            errors = []
            for attempt in range(3):                        # the template retries the exit 3x
                try:
                    out["snapshot_restore"] = _common.request_snapshot_restore(self.controller, entered=self.entered,
                                                                               monitor=self.monitor)
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"attempt {attempt + 1}: {type(exc).__name__}: {exc}")
                    self.clock.sleep(2.0)
            if errors:
                out["snapshot_restore_errors"] = errors
            self.entered = False
            self.clock.sleep(2.0)
        still_open, status = _common.calibration_session_open(self.controller)
        out["calibration_session_open_after"] = still_open
        if still_open:
            self.anomaly("calibration_session_still_open", status=status,
                         note="DesktopLUT is STILL in calibration mode — exit it with restore (calibration.exit "
                              "restore_snapshot=true) before anything else; no fixups were attempted")
        if self.pre_state is not None:
            out.update(self.verify_unchanged(fix=not still_open))
        self.evidence["restore"] = out
        return out

    def verify_unchanged(self, *, fix: bool) -> dict[str, Any]:
        post = self.snapshot()
        diff = stack_diff(self.pre_state, post)
        out: dict[str, Any] = {"post_state": post, "diff": diff, "fixups": []}
        if fix and any(c["what"].startswith("mhc_") for c in diff["changed"]):
            # cube / layer fixups would re-bake WB/GS onto the WRONG (identity) MHC — leave it to the operator
            out["fixups"].append("skipped: the MHC itself is not the pre-probe one")
            fix = False
        if fix and diff["changed"]:
            for ch in diff["changed"]:
                mon_s, md = ch["pair"].split(":")
                try:
                    if ch["what"] == "cube_path" and ch["before"]:
                        self.controller.set_3dlut(int(mon_s), md, ch["before"])
                        out["fixups"].append(f"re-set the {ch['pair']} cube {ch['before']}")
                    elif ch["what"] == "cube_path":
                        self.controller.clear_3dlut(int(mon_s), md)
                        out["fixups"].append(f"cleared the {ch['pair']} cube {ch['after']} (none was loaded before)")
                    elif ch["what"].startswith("layer:"):
                        n = ch["what"].split(":", 1)[1]
                        self.controller.set_layers(int(mon_s), md, **{n: bool(ch["before"])})
                        out["fixups"].append(f"{ch['pair']} layer {n} -> {ch['before']}")
                except Exception as exc:  # noqa: BLE001
                    out["fixups"].append(f"FAILED {ch}: {type(exc).__name__}: {exc}")
            post = self.snapshot()
            diff = stack_diff(self.pre_state, post)
            out.update({"post_state_after_fixups": post, "diff_after_fixups": diff})
        out["unchanged"] = not diff["changed"]
        if diff["changed"]:
            mhc = [c for c in diff["changed"] if c["what"].startswith("mhc_")]
            self.anomaly("stack_not_restored", changed=diff["changed"],
                         note=("the MHC profile is not the pre-probe one — re-apply it from the pre-probe ini backup "
                               f"({self.evidence.get('ini_backup') or 'DesktopLUT.ini'})" if mhc else
                               "restore the listed items by hand"))
        return out

    # -- meter ------------------------------------------------------------------------------------
    def open_meter(self, geometry: Geometry, *, panel: Optional[SimPanel] = None) -> None:
        self.geometry = geometry
        bd = int(self.args.bit_depth)
        from dlc.measure_loop import Reading
        if self.simulate:
            self.presenter = SimPresenter(self.clock, float(self.args.present_dwell_s))
            self.panel = panel
            meter_norm = (geometry.meter[0] / geometry.width, geometry.meter[1] / geometry.height)
            pres, clk = self.presenter, self.clock

            def measure(patch):
                pres.show(patch)
                frame = pres.last
                since = clk.now() - (pres.last_paint_t if pres.last_paint_t is not None else clk.now())
                xyz = panel.read(frame, meter_norm, since)
                clk.sleep(est_read_s(xyz[1]))
                return Reading(xyz=xyz, ok=True)
            self.read_fn = make_reader(self.presenter, measure, bd)
            self.evidence["meter"] = {"simulated": True}
            return
        from dlc.argyll import Argyll, SpotreadRequest
        from dlc.calibrate import correction_store_path, resolve_correction
        from dlc.correction_store import CorrectionStore
        from dlc.measure_loop import make_persistent_spotread_meter
        from dlc.measure_rgbw import resolve_spotread_instrument_port
        prof = self.profile()
        argyll = Argyll(Path(prof.paths["argyll"]) / "spotread.exe")
        port, info = resolve_spotread_instrument_port(argyll, prof.meter.argyll_port)
        store = CorrectionStore.load(correction_store_path(prof, ROOT))
        corr = resolve_correction(prof, store, prof.display_for(self.monitor).name, self.mode)
        host, _, srv_port = str(self.args.dogegen_server or "127.0.0.1:28930").partition(":")
        self.presenter = make_hw_presenter(host or "127.0.0.1", int(srv_port or 28930), self.clock,
                                           float(self.args.present_dwell_s))
        if not self.presenter.ping():
            raise Refusal(f"dogegen daemon not reachable at {host}:{srv_port} — start it in its own terminal: "
                          f"python -m dlc.dogegen_server --mode {self.mode} --bit-depth {bd} --monitor {self.monitor}")
        self.meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(corr.file) if corr.file else None))
        measure = make_persistent_spotread_meter(presenter=self.presenter, persistent=self.meter)
        self.read_fn = make_reader(self.presenter, measure, bd)
        self.evidence["meter"] = {"port": port, "resolve": info, "correction": corr.as_dict(),
                                  "display": prof.display_for(self.monitor).name}
        if corr.warning:
            self.evidence["warnings"].append(corr.warning)

    def paint(self, shapes, why: str) -> None:
        if self.presenter is None:
            return
        try:
            self.presenter.paint(shapes)
            self.event("frame", tier="stream", why=why, shapes=[[list(c), list(r)] for c, r in shapes])
        except Exception as exc:  # noqa: BLE001
            self.log(f"   [{why}] paint failed: {exc}")

    def park(self) -> None:
        self.paint(full((0, 0, 0)), "park_black")

    def idle(self) -> None:
        mode = getattr(self.args, "idle_between", "dim")
        if mode == "none" or self.presenter is None:
            return
        self.paint(full((0, 0, 0)) if mode == "black" or self.idle_code is None else full(self.idle_code), f"idle_{mode}")

    def close(self) -> None:
        self.park()
        for c in (getattr(self.meter, "close", None), getattr(self.presenter, "close", None)):
            try:
                if c:
                    c()
            except Exception:  # noqa: BLE001
                pass

    # -- reads -------------------------------------------------------------------------------------
    def _log_read(self, row: dict[str, Any]) -> None:
        with open(self.reads_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":"), default=float) + "\n")

    def measure_patch(self, p: Patch, *, state: str = "") -> PatchResult:
        """Paint (if the frame changed) and read until a settled tail exists (:func:`settled_tail`: the
        shortest suffix of >= 4 good reads spanning >= ``--settle-min-span-s`` that passes the spread /
        drift test against the PRIOR read σ), then keep reading until the tail holds the needed reads
        (>= 5 below 1 nit, >= 3 otherwise). Every read goes to reads.jsonl — a read spotread flags
        (under-range / unreliable warning, garbled line) is logged but never used. Unsettled within
        ``--settle-max-s`` = FLAGGED, the last reads kept. A dead meter raises :class:`MeterDown`."""
        g = self.geometry
        a = self.args
        ms = getattr(a, "settle_min_span_s", None)
        min_span = SETTLE_MIN_SPAN_S if ms is None else float(ms)
        reads: list[dict[str, Any]] = []
        ok_idx: list[int] = []
        kept: list[int] = []
        settled = False
        st0: Optional[int] = None
        t_show = None
        fails = 0
        err = None
        while True:
            if self.cancel_requested():
                err = "cancelled"
                break
            t_start = self.clock.now()
            xyz, ok, e, fault = self.read_fn(f"{p.phase}:{p.name}{(' [' + state + ']') if state else ''}", p.shapes, p.field)
            t_end = self.clock.now()
            wall = datetime.now().isoformat(timespec="milliseconds")
            if t_show is None:
                lp = getattr(self.presenter, "last_paint_t", None)
                t_show = lp if lp is not None else t_start
            self.n_reads += 1
            i = len(reads)
            # t_s / since_paint_s = the END of the integration (the settle test's time axis); the start is
            # clamped at the paint (the first read's duration includes the paint + present dwell)
            row = {"t": wall, "t_s": round(t_end - self.t0, 3), "since_paint_s": round(t_end - t_show, 3),
                   "since_paint_start_s": round(max(0.0, t_start - t_show), 3),
                   "read_s": round(t_end - t_start, 3), "phase": p.phase, "patch": p.name, "state": state, "read_index": i,
                   "field_code": list(p.field), "shapes_px": p.geometry_px(g) if g else None,
                   "xyz": list(xyz) if xyz else None, "ok": bool(ok), "error": e, "meter_fault": fault}
            reads.append(row)
            self._log_read(row)
            if fault in ("self_heal_exhausted", "closed"):
                raise MeterDown(f"the meter is down ({fault}): {e}")
            if not ok:
                fails += 1
                if fails >= 3:
                    err = f"3 consecutive failed / flagged reads ({e})"
                    break
                continue
            fails = 0
            ok_idx.append(i)
            ts = [reads[j]["t_s"] for j in ok_idx]
            xs = [reads[j]["xyz"] for j in ok_idx]
            if len(ok_idx) >= SETTLE_REQUIRED + 1:
                st = None
                if st0 is not None:                       # grow the settled tail while it still passes
                    ok_grow, _ = tail_settled(ts[st0:], xs[st0:], min_span_s=min_span,
                                              rel_tol=a.settle_rel_tol, abs_tol=a.settle_abs_tol)
                    st = st0 if ok_grow else None
                if st is None:                            # (re)search the shortest settled suffix
                    st = settled_tail(ts, xs, min_reads=SETTLE_REQUIRED + 1, min_span_s=min_span,
                                      rel_tol=a.settle_rel_tol, abs_tol=a.settle_abs_tol)
                st0 = st
                settled = st is not None
                kept = ok_idx[st:] if st is not None else []
                if st is not None:
                    ys = [reads[j]["xyz"][1] for j in kept]
                    if len(kept) >= reads_needed(sum(ys) / len(ys), p.min_reads):
                        break
            if (self.clock.now() - t_show) > a.settle_max_s or len(reads) >= a.settle_max_reads:
                if not settled:
                    ys = [reads[j]["xyz"][1] for j in ok_idx[-(SETTLE_REQUIRED + 1):]]
                    kept = ok_idx[-max(reads_needed(sum(ys) / len(ys), p.min_reads), SETTLE_REQUIRED + 1):]
                break
        res = PatchResult(p, None, len(kept), settled, None, reads, kept, state=state, error=err)
        if kept:
            xs = [reads[j]["xyz"] for j in kept]
            res.xyz = tuple(sum(v[c] for v in xs) / len(xs) for c in range(3))
            ys = [v[1] for v in xs]
            m = sum(ys) / len(ys)
            res.sd_y = round(math.sqrt(sum((y - m) ** 2 for y in ys) / max(len(ys) - 1, 1)), 6) if len(ys) > 1 else None
            res.settle_s = round(reads[kept[0]]["since_paint_start_s"], 3)
        flagged = sum(1 for r in reads if r["xyz"] is not None and not r["ok"])
        if flagged:
            self.anomaly("flagged_reads", phase=p.phase, patch=p.name, state=state, flagged=flagged,
                         errors=sorted({str(r["error"]) for r in reads if not r["ok"]})[:3],
                         note="spotread warned (under-range / unreliable / garbled): logged, not used")
        if not settled and err != "cancelled" and kept:
            self.anomaly("unsettled", phase=p.phase, patch=p.name, state=state, reads=len(reads),
                         waited_s=round(self.clock.now() - (t_show or self.clock.now()), 1),
                         note="no settled tail within --settle-max-s; the last reads are kept, FLAGGED")
        if not kept and err != "cancelled":
            self.no_read_streak += 1
            self.anomaly("no_read", phase=p.phase, patch=p.name, state=state, error=err)
            if self.no_read_streak >= 3:
                raise MeterDown(f"3 consecutive patches without a usable read (last: {err})")
        elif kept:
            self.no_read_streak = 0
        self.event("patch_measured", tier="stream", phase=p.phase, patch=p.name, state=state,
                   y=res.y, n_kept=res.n_kept, settled=settled, settle_s=res.settle_s, reads=len(reads))
        self.log(f"   {p.phase}:{p.name:<28}{(' [' + state + ']') if state else '':<11} Y={res.y if res.y is not None else float('nan'):10.5f}"
                 f"  n={res.n_kept}{'' if settled else ' UNSETTLED'}  settle {res.settle_s}s  ({len(reads)} reads)")
        return res

    def progress(self, phase: str, i: int, n: int, results: Sequence[PatchResult], last: str) -> None:
        """check_in evidence packets: 25/50/75 % of the phase + every 180 s. Non-blocking, no verdict."""
        frac = (i + 1) / max(n, 1)
        triggers = [f"progress_{int(f * 100)}" for f in CHECKIN_FRACTIONS if frac >= f and (phase, f) not in self._ci_fired]
        for f in CHECKIN_FRACTIONS:
            if frac >= f:
                self._ci_fired.add((phase, f))
        if self.clock.now() - self._ci_last >= CHECKIN_EVERY_S:
            triggers.append("timed")
        if not triggers:
            return
        self._ci_seq += 1
        self._ci_last = self.clock.now()
        ys = [r.y for r in results if r.y is not None]
        settle = sorted(r.settle_s for r in results if r.settle_s is not None)
        unsettled = [f"{r.patch.name}{('[' + r.state + ']') if r.state else ''}" for r in results if not r.settled]
        sds = [(r.sd_y / r.y, r.patch.name) for r in results if r.sd_y and r.y and r.y > 0]
        worst_sd = max(sds) if sds else None
        # progress / anomalies_total / evidence = the fields runs/_watch_events.py prints on its one line
        self.event("check_in", phase=phase, seq=self._ci_seq, trigger=",".join(triggers), patches=i + 1, of=n,
                   progress=f"{phase} {i + 1}/{n}", anomalies_total=len(self.anomalies),
                   evidence={"last": last, "y_range": [min(ys), max(ys)] if ys else None, "unsettled": len(unsettled),
                             "settle_s_max": settle[-1] if settle else None},
                   reads_total=self.n_reads, elapsed_s=round(self.clock.now() - self.t0, 1), last=last,
                   max_nits=max(ys) if ys else None, min_nits=min(ys) if ys else None,
                   settle_s_median=settle[len(settle) // 2] if settle else None, settle_s_max=settle[-1] if settle else None,
                   unsettled=unsettled[-10:], unsettled_count=len(unsettled),
                   worst_rel_sd={"patch": worst_sd[1], "rel_sd": round(worst_sd[0], 5)} if worst_sd else None,
                   anomalies=self.anomalies[-10:], anomaly_count=len(self.anomalies))

    def transport_check(self, expected_nits: float) -> dict[str, Any]:
        """One start-of-session sanity read: a mid-grey 600-px square ON the meter, on black (background + one
        rectangle — the frame type every probe uses). Refuses when the read is outside expected ×/÷ 1.5: a
        daemon at the wrong ``--bit-depth`` (an 8-bit daemon clips a 10-bit code to full white), the wrong
        monitor, a misplaced meter, or a transport that drops the rectangle."""
        g = self.geometry
        code = self._mid_code
        p = Patch("transport:mid600", "transport", framed(g, (0, 0, 0), grey(code), g.centred(600, 600)), grey(code),
                  meta={"expected_nits": expected_nits})
        r = self.measure_patch(p)
        ratio = (r.y / expected_nits) if (r.y and expected_nits) else None
        rec = {"code": code, "expected_nits": expected_nits, "y": r.y, "ratio": ratio}
        self.evidence["transport_check"] = rec
        self.park()
        if ratio is None or not (1 / 1.5 <= ratio <= 1.5):
            raise Refusal(f"transport check: a mid-grey 600-px square read {r.y} nit, expected ~{expected_nits:.1f} — wrong "
                          "daemon --bit-depth / monitor, the meter off the spot, or the rectangle not drawn "
                          "(--skip-transport-check to override)")
        return rec

    def run_patches(self, phase: str, patches: Sequence[Patch], *, state: str = "",
                    before_each: Optional[Callable[[Patch], None]] = None,
                    results: Optional[list] = None, idle: bool = True) -> list[PatchResult]:
        out = results if results is not None else []
        for i, p in enumerate(patches):
            if self.cancel_requested():
                self.log("[cancel] control.json cancel honoured")
                self.evidence["cancelled"] = True
                break
            if before_each:
                before_each(p)
            r = self.measure_patch(p, state=state)
            out.append(r)
            if idle:
                self.idle()
            self.progress(phase, i, len(patches), out, p.name)
        return out

    def elapsed(self) -> float:
        return self.clock.now() - self.t0


def run_transport_check(s: "ProbeSession", *, sdr_white_nits: float = 120.0, sdr_gamma: float = 2.2) -> None:
    """The mid-grey sanity read (skipped with --skip-transport-check): HDR = PQ code of 100 nit, SDR = 50 %."""
    if getattr(s.args, "skip_transport_check", False):
        s.evidence["transport_check"] = {"skipped": True}
        return
    bd = int(s.args.bit_depth)
    if s.mode == "HDR":
        s._mid_code = pq_code(100.0, bd)
        expected = pq_nits(s._mid_code, bd)
    else:
        s._mid_code = int(round(max_code(bd) / 2))
        expected = sdr_nits(s._mid_code, sdr_white_nits, sdr_gamma, bd)
    s.transport_check(expected)


# ----------------------------------------------------------------------------- phase outputs
def phase_outputs(s: ProbeSession, phase: str, results: Sequence[PatchResult], *, bit_depth: int, title: str,
                  split_by_cond: bool = True, ti3_layout: str = "flat", extra: Optional[dict] = None,
                  notes: Sequence[str] = ()) -> dict[str, Any]:
    """Write ``<run>/<phase>/<phase>.json`` (+ one ``.ti3`` per condition). ``ti3_layout='measurements'``
    writes ``<run>/<phase>/measurements/raw.ti3`` — the layout ``pa_additivity.py <dir>`` reads."""
    d = s.root / phase
    d.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list] = {}
    for r in results:
        if r.xyz is None:
            continue
        k = (r.patch.cond or r.state or "") if split_by_cond else ""
        groups.setdefault(k, []).append((r.patch.ti3_rgb or r.patch.field, r.xyz))
    ti3s = {}
    for k, rows in groups.items():
        if ti3_layout == "measurements":
            path = d / "measurements" / ("raw.ti3" if not k else f"raw_{k}.ti3")
        else:
            path = d / f"{phase}{('_' + k) if k else ''}.ti3"
        write_ti3(path, rows, bit_depth=bit_depth, title=f"{title}{(' — ' + k) if k else ''}", notes=notes)
        ti3s[k or "all"] = str(path)
    body = {"phase": phase, "probe": s.probe, "monitor": s.monitor, "mode": s.mode, "bit_depth": bit_depth,
            "geometry": s.geometry.as_dict() if s.geometry else None, "ti3": ti3s,
            "complete": not s.evidence.get("cancelled"), "results": [r.as_dict(s.geometry) for r in results]}
    body.update(extra or {})
    atomic_write_text(d / f"{phase}.json", json.dumps(body, indent=1, default=float))
    s.evidence.setdefault("outputs", {})[phase] = {"json": str(d / f"{phase}.json"), "ti3": ti3s}
    s.event("phase_done", phase=phase, patches=len(results), ti3=ti3s,
            unsettled=sum(1 for r in results if not r.settled), no_read=sum(1 for r in results if r.xyz is None))
    return body


def plan_summary(patches: Sequence[Patch], panel: SimPanel, g: Geometry, *, states: int = 1,
                 min_span_s: float = SETTLE_MIN_SPAN_S) -> dict[str, Any]:
    """Patch count + an estimated duration from the read-time model and the synthetic panel's
    expected luminance (dark reads dominate: ~7 s each below 0.1 nit)."""
    meter_norm = (g.meter[0] / g.width, g.meter[1] / g.height)
    tot, dark = 0.0, 0
    rows = []
    for p in patches:
        y = panel.expected(p.shapes, meter_norm)[1]
        dark += y < SUBNIT_NITS
        t = est_patch_s(y, min_reads=p.min_reads, min_span_s=min_span_s) * states
        tot += t
        rows.append((p, y, t))
    return {"patches": len(patches) * states, "subnit": dark * states, "est_s": tot, "rows": rows}


def print_plan(title: str, patches: Sequence[Patch], panel: SimPanel, g: Geometry, *, states: int = 1,
               min_span_s: float = SETTLE_MIN_SPAN_S) -> float:
    ps = plan_summary(patches, panel, g, states=states, min_span_s=min_span_s)
    print(f"== {title}: {ps['patches']} patch reads ({ps['subnit']} sub-nit), est {fmt_min(ps['est_s'])}")
    for p, y, t in ps["rows"]:
        gap = ""
        if len(p.shapes) > 1:
            gap = f" gap {g.gap_px(g.px(p.shapes[1][1])):.0f}px"
        print(f"   {p.name:<26} field {str(list(p.field)):<16} cond {p.cond or '-':<9} ~{y:9.4f} nit  ~{t:5.1f}s{gap}")
    return ps["est_s"]


def finish(s: ProbeSession, status: str, *, operator_note: str = "") -> int:
    s.evidence["status"] = status
    if operator_note:
        s.evidence["operator_note"] = operator_note
    path = s.save_evidence()
    s.event("probe_done", status=status, reads=s.n_reads, elapsed_s=round(s.elapsed(), 1),
            anomalies=len(s.anomalies), evidence=str(path), operator_note=operator_note or None)
    s.event("run_done", status=status, anomalies_total=len(s.anomalies))   # runs/_watch_events.py stops on it
    print(json.dumps({"status": status, "run_dir": str(s.root), "reads": s.n_reads,
                      "elapsed_s": round(s.elapsed(), 1), "anomalies": len(s.anomalies)}))
    if operator_note:
        print(f"\n>>> OPERATOR: {operator_note}", file=sys.stderr)
    return 0 if status == "ok" else (2 if status.startswith("refused") else 1)
